from __future__ import annotations

import asyncio
from typing import Any

from student_agent.agents.payment_agent import (
    assess_payment,
    collect_payment_evidence,
    select_episode,
)

# Fixtures mirror real MCP responses for L3A_CASE_005..009: each case mixes its real
# payment episode with a decoy episode (months earlier or after opened_at).

PAY_REF = "ev_" + "p" * 24
TIMELINE_REF = "ev_" + "t" * 24
REFUND_REF = "ev_" + "r" * 24


def row(payment_type: str, value: str, sequential: str = "1") -> dict[str, str]:
    return {
        "payment_sequential": sequential,
        "payment_type": payment_type,
        "payment_installments": "1",
        "payment_value": value,
    }


def event(at: str, amount: str, event_type: str = "captured", status: str = "confirmed"):
    return {"event_at": at, "event_type": event_type, "amount_brl": amount, "status": status}


def evidence(
    rows: list[dict[str, str]],
    events: list[dict[str, str]],
    refunds: list[dict[str, str]] | None,
) -> dict[str, Any]:
    refs_by_domain = {"payments": [PAY_REF], "payment_timeline": [TIMELINE_REF]}
    if refunds is not None:
        refs_by_domain["refund_timeline"] = [REFUND_REF]
    return {
        "payments": rows,
        "payment_timeline": {"payments": rows, "events": events},
        "refund_timeline": {"events": refunds} if refunds is not None else None,
        "refs_by_domain": refs_by_domain,
    }


def test_select_episode_drops_future_and_old_events() -> None:
    events = [
        event("2018-05-25T10:00:00-03:00", "35.00"),
        event("2018-05-25T12:00:00-03:00", "35.00", "reconciliation_mismatch", "open"),
        event("2018-09-06T10:00:00-03:00", "89.00"),
    ]
    episode = select_episode(events, "2018-06-06T09:00:00-03:00")
    assert [e["amount_brl"] for e in episode] == ["35.00", "35.00"]


def test_case_005_split_payment_ignores_old_failed_refund() -> None:
    result = assess_payment(
        evidence(
            [
                row("credit_card", "44.50"),
                row("voucher", "44.50", "2"),
                row("credit_card", "52.00"),
            ],
            [
                event("2018-04-23T10:00:00-03:00", "44.50"),
                event("2018-04-23T11:00:00-03:00", "44.50"),
                event("2018-01-07T10:00:00-03:00", "52.00"),
            ],
            [event("2018-01-18T09:00:00-03:00", "52.00", "refund_requested", "failed")],
        ),
        opened_at="2018-05-05T09:00:00-03:00",
        order_total=89.0,
    )
    assert result["signal"] == "valid_split_payment"
    assert result["paid_brl"] == 89.0
    assert result["refund_brl"] == 0.0
    assert [r["payment_type"] for r in result["payment_rows"]] == ["credit_card", "voucher"]
    assert REFUND_REF not in result["evidence_refs"]


def test_case_006_open_reconciliation_is_payment_mismatch() -> None:
    result = assess_payment(
        evidence(
            [row("credit_card", "35.00"), row("credit_card", "89.00")],
            [
                event("2018-05-25T10:00:00-03:00", "35.00"),
                event("2018-05-25T12:00:00-03:00", "35.00", "reconciliation_mismatch", "open"),
                event("2018-09-06T10:00:00-03:00", "89.00"),
            ],
            [event("2018-09-17T09:00:00-03:00", "89.00", "refund_requested", "pending")],
        ),
        opened_at="2018-06-06T09:00:00-03:00",
    )
    assert result["signal"] == "payment_mismatch"
    assert result["refund_status"] is None


def test_case_007_duplicate_charge_without_refund_timeline() -> None:
    rows = [row("credit_card", "64.00"), row("voucher", "64.00", "2")] * 2
    result = assess_payment(
        evidence(
            rows,
            [
                event("2018-06-25T10:00:00-03:00", "64.00"),
                event("2018-06-25T11:00:00-03:00", "64.00"),
                event("2018-08-05T10:00:00-03:00", "64.00"),
                event("2018-08-05T11:00:00-03:00", "64.00"),
            ],
            None,
        ),
        opened_at="2018-07-07T09:00:00-03:00",
        order_total=89.0,
    )
    assert result["signal"] == "duplicate_charge"
    assert result["paid_brl"] == 128.0
    assert result["refund_brl"] == 64.0


def test_two_equal_captures_without_order_total_is_not_guessed() -> None:
    result = assess_payment(
        evidence(
            [row("credit_card", "64.00"), row("voucher", "64.00", "2")],
            [
                event("2018-06-25T10:00:00-03:00", "64.00"),
                event("2018-06-25T11:00:00-03:00", "64.00"),
            ],
            None,
        ),
        opened_at="2018-07-07T09:00:00-03:00",
    )
    assert result["signal"] == "insufficient_evidence"


def test_case_008_pending_refund_ignores_older_mismatch() -> None:
    result = assess_payment(
        evidence(
            [row("credit_card", "89.00"), row("credit_card", "35.00")],
            [
                event("2018-07-27T10:00:00-03:00", "89.00"),
                event("2018-07-04T10:00:00-03:00", "35.00"),
                event("2018-07-04T12:00:00-03:00", "35.00", "reconciliation_mismatch", "open"),
            ],
            [event("2018-08-07T09:00:00-03:00", "89.00", "refund_requested", "pending")],
        ),
        opened_at="2018-08-08T09:00:00-03:00",
    )
    assert result["signal"] == "refund_pending"
    assert result["refund_brl"] == 89.0
    assert [r["payment_value"] for r in result["payment_rows"]] == ["89.00"]
    assert REFUND_REF in result["evidence_refs"]


def test_case_009_failed_refund_ignores_older_split() -> None:
    result = assess_payment(
        evidence(
            [
                row("credit_card", "52.00"),
                row("credit_card", "44.50"),
                row("voucher", "44.50", "2"),
            ],
            [
                event("2018-08-28T10:00:00-03:00", "52.00"),
                event("2018-06-03T10:00:00-03:00", "44.50"),
                event("2018-06-03T11:00:00-03:00", "44.50"),
            ],
            [event("2018-09-08T09:00:00-03:00", "52.00", "refund_requested", "failed")],
        ),
        opened_at="2018-09-09T09:00:00-03:00",
        order_total=89.0,
    )
    assert result["signal"] == "refund_failed"
    assert result["refund_brl"] == 52.0


def test_missing_timeline_is_insufficient_evidence() -> None:
    result = assess_payment({"refs_by_domain": {}}, opened_at="2018-07-07T09:00:00-03:00")
    assert result["signal"] == "insufficient_evidence"
    assert result["evidence_refs"] == []


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def call(self, tool_name: str, *, case_id: str, order_id: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, order_id))
        if tool_name == "get_refund_timeline":
            raise RuntimeError("MCP tool get_refund_timeline failed: no refund")
        return {"evidence_ref": f"ev_{tool_name}_{'x' * 12}", "data": {"tool": tool_name}}


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> None:
        self.events.append(event)


def test_collect_records_refs_traces_and_tolerates_missing_refund() -> None:
    gateway, trace = FakeGateway(), FakeTrace()
    tools = {"get_order_payments", "get_payment_timeline", "get_refund_timeline", "get_order"}
    result = asyncio.run(
        collect_payment_evidence(
            case_id="L3A_CASE_007",
            order_id="order-1",
            available_tools=tools,
            gateway=gateway,
            trace=trace,
        )
    )
    assert [call[0] for call in gateway.calls] == [
        "get_order_payments",
        "get_payment_timeline",
        "get_refund_timeline",
    ]
    assert all(call[1] == "L3A_CASE_007" for call in gateway.calls)
    assert result["refund_timeline"] is None
    assert result["unavailable_tools"] == ["get_refund_timeline"]
    assert result["evidence_refs"] == [
        "ev_get_order_payments_xxxxxxxxxxxx",
        "ev_get_payment_timeline_xxxxxxxxxxxx",
    ]
    assert [e["event_type"] for e in trace.events] == ["tool_result_consumed"] * 2
    assert all(e["actor"] == "payment-agent" for e in trace.events)
    assert trace.events[0]["evidence_refs"] == ["ev_get_order_payments_xxxxxxxxxxxx"]


def test_collect_skips_tools_not_discovered() -> None:
    gateway, trace = FakeGateway(), FakeTrace()
    result = asyncio.run(
        collect_payment_evidence(
            case_id="L3A_CASE_005",
            order_id="order-1",
            available_tools={"get_order_payments"},
            gateway=gateway,
            trace=trace,
        )
    )
    assert [call[0] for call in gateway.calls] == ["get_order_payments"]
    assert result["payment_timeline"] is None
