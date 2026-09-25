from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter

ACTOR = "payment-agent"
TOLERANCE = Decimal("0.01")
# Events closer together than this belong to the same payment episode.
EPISODE_GAP = timedelta(days=1)

PAYMENT_TOOLS = (
    ("get_order_payments", "payments"),
    ("get_payment_timeline", "payment_timeline"),
    ("get_refund_timeline", "refund_timeline"),
)


async def collect_payment_evidence(
    *,
    case_id: str,
    order_id: str,
    available_tools: set[str],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    """Collect payment and refund evidence without making claims about the case."""
    evidence: dict[str, Any] = {
        "payments": None,
        "payment_timeline": None,
        "refund_timeline": None,
        "evidence_refs": [],
        "refs_by_domain": {},
        "unavailable_tools": [],
    }

    for tool_name, result_key in PAYMENT_TOOLS:
        if tool_name not in available_tools:
            continue
        try:
            response = await gateway.call(tool_name, case_id=case_id, order_id=order_id)
        except RuntimeError:
            # get_refund_timeline fails when the order has no refund. Record the gap
            # instead of inventing data.
            evidence["unavailable_tools"].append(tool_name)
            continue
        evidence[result_key] = response["data"]
        evidence_ref = response["evidence_ref"]
        evidence["evidence_refs"].append(evidence_ref)
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=ACTOR,
            tool_name=tool_name,
            evidence_refs=[evidence_ref],
        )
        evidence["refs_by_domain"][result_key] = [evidence_ref]

    return evidence


def _money(value: Any) -> Decimal:
    return Decimal(str(value))


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def select_episode(events: list[dict[str, Any]], opened_at: str) -> list[dict[str, Any]]:
    """Return the latest cluster of events that happened before the case was opened.

    MCP data mixes the case's real episode with a decoy episode (months earlier, or after
    opened_at) under the same order_id. Keep this rule in one place so it is easy to change.
    """
    cutoff = _time(opened_at)
    past = sorted(
        (event for event in events if _time(event["event_at"]) <= cutoff),
        key=lambda event: _time(event["event_at"]),
    )
    if not past:
        return []
    episode = [past[-1]]
    for event in reversed(past[:-1]):
        if _time(episode[0]["event_at"]) - _time(event["event_at"]) > EPISODE_GAP:
            break
        episode.insert(0, event)
    return episode


def _episode_rows(
    rows: list[dict[str, Any]],
    events: list[dict[str, Any]],
    episode: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Link undated payment rows to the episode via the captured events, in order."""
    captures = [event for event in events if event["event_type"] == "captured"]
    if len(captures) != len(rows):
        return []
    linked = []
    for row, capture in zip(rows, captures, strict=True):
        if _money(row["payment_value"]) != _money(capture["amount_brl"]):
            return []
        if any(capture is event for event in episode):
            linked.append(row)
    return linked


def _episode_refund(
    refund_timeline: dict[str, Any] | None,
    opened_at: str,
    episode: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Latest refund between the episode start and opened_at for an amount captured in it."""
    if not refund_timeline:
        return None
    start, cutoff = _time(episode[0]["event_at"]), _time(opened_at)
    captured = {_money(e["amount_brl"]) for e in episode if e["event_type"] == "captured"}
    candidates = [
        event
        for event in refund_timeline.get("events", [])
        if start <= _time(event["event_at"]) <= cutoff and _money(event["amount_brl"]) in captured
    ]
    return max(candidates, key=lambda event: _time(event["event_at"]), default=None)


def assess_payment(
    evidence: dict[str, Any], *, opened_at: str, order_total: float | None = None
) -> dict[str, Any]:
    """Classify the payment side of a case from collected evidence.

    `order_total` (price + freight of the case's item) comes from the order agent; it is the
    only thing that tells a valid split payment apart from a duplicate charge.
    """
    refs = evidence["refs_by_domain"]
    events = (evidence.get("payment_timeline") or {}).get("events", [])
    result: dict[str, Any] = {
        "signal": "insufficient_evidence",
        "paid_brl": 0.0,
        "refund_brl": 0.0,
        "refund_status": None,
        "payment_rows": [],
        "evidence_refs": [],
    }
    episode = select_episode(events, opened_at)
    if not episode:
        return result

    captures = [event for event in episode if event["event_type"] == "captured"]
    paid = sum((_money(event["amount_brl"]) for event in captures), Decimal(0))
    payment_refs = refs.get("payments", []) + refs.get("payment_timeline", [])
    result.update(
        signal=None,
        paid_brl=float(paid),
        payment_rows=_episode_rows(evidence.get("payments") or [], events, episode),
        evidence_refs=payment_refs,
    )

    refund = _episode_refund(evidence.get("refund_timeline"), opened_at, episode)
    if refund is not None and refund["status"] in ("pending", "failed"):
        result.update(
            signal=f"refund_{refund['status']}",
            refund_status=refund["status"],
            refund_brl=float(_money(refund["amount_brl"])),
            evidence_refs=payment_refs + refs.get("refund_timeline", []),
        )
    elif any(
        event["event_type"] == "reconciliation_mismatch" and event["status"] == "open"
        for event in episode
    ):
        result["signal"] = "payment_mismatch"
    elif len(captures) > 1:
        if order_total is None:
            result["signal"] = "insufficient_evidence"
        elif abs(paid - _money(order_total)) <= TOLERANCE:
            result["signal"] = "valid_split_payment"
        else:
            seen: set[Decimal] = set()
            duplicated = Decimal(0)
            for event in captures:
                amount = _money(event["amount_brl"])
                if amount in seen:
                    duplicated += amount
                seen.add(amount)
            result.update(signal="duplicate_charge", refund_brl=float(duplicated))
    return result
