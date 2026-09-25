from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

REQUIRED = ("get_order", "get_order_items", "get_payment_timeline", "get_shipment_summary")


def instant(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except (AttributeError, ValueError):
        return None


def money(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def events(value: Any, start: datetime | None, end: datetime | None) -> list[dict[str, Any]]:
    source = value.get("events") if isinstance(value, dict) else []
    return [event for event in rows(source)
            if (at := instant(event.get("event_at"))) and end and at <= end
            and (start is None or at >= start)]


def refs(evidence: dict[str, dict[str, Any]], *tools: str) -> list[str]:
    return list(dict.fromkeys(evidence[name]["evidence_ref"] for name in tools if name in evidence))


def ids(source: list[dict[str, Any]], key: str) -> list[str]:
    return list(dict.fromkeys(row[key] for row in source
                              if isinstance(row.get(key), str) and row[key]))[:20]


def infer_issue(
    order: dict[str, Any], items: list[dict[str, Any]], payments: dict[str, Any],
    shipment: dict[str, Any], refunds: dict[str, Any], opened: datetime | None,
) -> tuple[str, tuple[str, ...], float]:
    purchase = instant(order.get("order_purchase_timestamp"))
    payment_events = events(payments, purchase, opened)
    captures = [event for event in payment_events
                if event.get("event_type") == "captured" and event.get("status") == "confirmed"]
    status = order.get("order_status")
    if status in {"canceled", "unavailable"} and captures:
        tools = ("get_order", "get_order_items", "get_payment_timeline")
        if status == "unavailable":
            tools += ("get_sellers",)
        return f"{status}_order_paid", tools, 0.98

    refund_states = [event.get("status") for event in events(refunds, purchase, opened)
                     if event.get("event_type") in {"refund_requested", "refund_failed"}]
    if "failed" in refund_states:
        return "refund_failed", ("get_refund_timeline", "get_payment_timeline"), 0.98
    if "pending" in refund_states:
        return "refund_pending", ("get_refund_timeline", "get_payment_timeline"), 0.96

    if any(event.get("event_type") == "reconciliation_mismatch"
           and event.get("status") in {"open", "confirmed"} for event in payment_events):
        return "payment_mismatch", ("get_payment_timeline", "get_order_items"), 0.97

    expected = sum((money(row.get("price")) + money(row.get("freight_value"))
                    for row in items), Decimal(0))
    captured = sum((money(event.get("amount_brl")) for event in captures), Decimal(0))
    if len(captures) >= 2 and expected > 0 and captured > expected:
        return "duplicate_charge", ("get_payment_timeline", "get_order_items"), 0.95

    delivered = instant(shipment.get("delivered_customer_at")
                        or order.get("order_delivered_customer_date"))
    estimate = instant(shipment.get("estimated_delivery_at")
                       or order.get("order_estimated_delivery_date"))
    carrier = instant(shipment.get("delivered_carrier_at")
                      or order.get("order_delivered_carrier_date"))
    limits = [limit for row in items if (limit := instant(row.get("shipping_limit_date")))
              and (purchase is None or limit >= purchase)]
    limit = min(limits) if limits else None
    late = bool(estimate and opened and estimate < opened
                and (delivered is None or delivered > estimate))
    if late:
        actor = next((event.get("actor") for event in events(shipment, purchase, opened)
                      if event.get("event_type") == "delivered_late"), None)
        if actor == "seller" or (actor is None and carrier and limit and carrier > limit):
            return "late_delivery_seller", ("get_order", "get_order_items",
                                            "get_shipment_summary", "get_sellers"), 0.96
        return "late_delivery_logistics", ("get_order", "get_order_items",
                                           "get_shipment_summary"), 0.94

    if len(captures) > 1 and expected > 0 and captured == expected:
        return "valid_split_payment", ("get_payment_timeline", "get_order_items"), 0.95
    if order and captures and shipment:
        return "unsupported_claim", ("get_order", "get_payment_timeline",
                                      "get_shipment_summary"), 0.91
    return "insufficient_evidence", tuple(), 0.35


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    request = case.get("customer_request", {})
    order_id = request.get("claimed_order_id")
    opened = instant(case.get("opened_at"))
    available = set(await gateway.list_tools())
    evidence: dict[str, dict[str, Any]] = {}
    failures: list[str] = []

    async def obtain(actor: str, tool: str, **arguments: str) -> None:
        if tool not in available:
            failures.append(tool)
            return
        trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator",
                   target=actor, decision_code=tool.upper())
        try:
            for attempt in range(2):
                try:
                    result = await asyncio.wait_for(
                        gateway.call(tool, case_id=case_id, **arguments), timeout=35
                    )
                    break
                except TimeoutError:
                    if attempt:
                        raise
            if not isinstance(result, dict) or "evidence_ref" not in result:
                raise ValueError("invalid MCP evidence")
            evidence[tool] = result
            trace.emit(case_id=case_id, event_type="tool_result_consumed", actor=actor,
                       tool_name=tool, evidence_refs=[result["evidence_ref"]])
            trace.emit(case_id=case_id, event_type="handoff", actor=actor,
                       target="coordinator", decision_code="EVIDENCE_READY")
        except (RuntimeError, ValueError, TimeoutError):
            failures.append(tool)
            trace.emit(case_id=case_id, event_type="handoff", actor=actor,
                       target="coordinator", decision_code="EVIDENCE_UNAVAILABLE")

    if isinstance(order_id, str) and order_id:
        for tool, actor in (
            ("get_order", "order-agent"),
            ("get_order_items", "order-agent"),
            ("get_payment_timeline", "payment-agent"),
            ("get_shipment_summary", "shipment-agent"),
        ):
            await obtain(actor, tool, order_id=order_id)
        topics = {claim.get("topic") for claim in request.get("claims", [])
                  if isinstance(claim, dict)}
        if topics.intersection({"refund_pending", "refund_failed"}):
            await obtain("payment-agent", "get_refund_timeline", order_id=order_id)
        if topics.intersection({"unavailable_order_paid", "late_delivery_seller"}):
            await obtain("order-agent", "get_sellers", order_id=order_id)
    else:
        failures.extend(REQUIRED)
    version = case.get("policy_version")
    if isinstance(version, str) and version:
        await obtain("policy-agent", "get_policy", policy_version=version)

    data = {name: result.get("data") for name, result in evidence.items()}
    order = data.get("get_order") if isinstance(data.get("get_order"), dict) else {}
    purchase = instant(order.get("order_purchase_timestamp"))
    items = [row for row in rows(data.get("get_order_items"))
             if row.get("order_id") == order_id
             and (purchase is None or (limit := instant(row.get("shipping_limit_date")))
                  and limit >= purchase and (opened is None or limit <= opened))]
    payments = data.get("get_payment_timeline")
    shipment = data.get("get_shipment_summary")
    refunds = data.get("get_refund_timeline")
    issue, supporting, confidence = infer_issue(
        order, items, payments if isinstance(payments, dict) else {},
        shipment if isinstance(shipment, dict) else {},
        refunds if isinstance(refunds, dict) else {}, opened,
    )
    if any(tool not in evidence for tool in REQUIRED):
        issue, supporting, confidence = "insufficient_evidence", tuple(evidence), 0.3

    policy = data.get("get_policy")
    rules = policy.get("rules", {}) if isinstance(policy, dict) else {}
    rule = rules.get(issue, {}) if isinstance(rules, dict) else {}
    status = rule.get("case_status", "needs_investigation")
    amount = round(float(money(rule.get("refund_brl"))), 2) if rule else 0.0
    if issue == "insufficient_evidence" or not rule:
        status, amount, confidence = "needs_investigation", 0.0, min(confidence, 0.4)
    action = rule.get("recommended_action") if rule else "investigate_missing_evidence"
    seller_ids = ids(items, "seller_id")
    responsible = []
    for party in rule.get("responsible_parties", [])[:5]:
        if isinstance(party, dict):
            kind = party.get("party_type", "unknown")
            responsible.append({"party_type": kind,
                                "party_id": seller_ids[0] if kind == "seller" and seller_ids
                                else None})
    trace.emit(case_id=case_id, event_type="policy_decided", actor="policy-agent",
               decision_code=issue.upper(), evidence_refs=refs(evidence, "get_policy"))

    primary_refs = refs(evidence, *supporting)
    policy_refs = refs(evidence, "get_policy")
    entity_refs = refs(evidence, "get_order_items") if items else []
    all_refs = list(dict.fromkeys([*primary_refs, *entity_refs, *policy_refs]))[:30]
    captured = sum((money(event.get("amount_brl")) for event in events(
        payments if isinstance(payments, dict) else {}, purchase, opened)
        if event.get("event_type") == "captured" and event.get("status") == "confirmed"),
        Decimal(0))
    claim_results = []
    for claim in request.get("claims", [])[:5]:
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
            continue
        topic = claim.get("topic")
        if topic == "requested_full_refund":
            full_refund_issue = issue in {"canceled_order_paid", "unavailable_order_paid"}
            verdict = ("supported" if full_refund_issue and amount > 0
                       and money(amount) >= captured > 0
                       else "partially_supported" if amount > 0 else "unsupported")
            claim_refs = all_refs
        else:
            verdict = ("unsupported" if issue == "unsupported_claim" else
                       "supported" if topic == issue else
                       "insufficient_evidence" if issue == "insufficient_evidence" else
                       "unsupported")
            claim_refs = primary_refs
        claim_results.append({"claim_id": claim["claim_id"], "verdict": verdict,
                              "confidence": confidence, "evidence_refs": claim_refs})

    output = {
        "schema_version": "day09-l3a-output-v2", "case_id": case_id,
        "assessment": {"primary_issue": issue, "case_status": status,
                       "confidence": confidence},
        "affected_entities": {
            "order_ids": [order_id] if order.get("order_id") == order_id else [],
            "item_ids": ids(items, "order_item_id"), "seller_ids": seller_ids,
            "payment_references": [], "shipment_ids": [],
        },
        "claim_assessments": claim_results,
        "root_cause_analysis": {
            "ranked_causes": ([{"cause_code": issue.upper(), "rank": 1}]
                              if issue != "insufficient_evidence" else []),
            "responsible_parties": responsible,
        },
        "evidence_refs": all_refs, "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL", "recommended_refund_brl": amount,
            "refund_lines": ([{"reason_code": issue.upper(), "amount_brl": amount,
                              "entity_id": order_id}] if amount > 0 else []),
        },
        "resolution_actions": [action],
    }
    critical = set(REQUIRED) | {"get_policy"}
    if any(claim.get("topic") in {"refund_pending", "refund_failed"}
           for claim in request.get("claims", []) if isinstance(claim, dict)):
        critical.add("get_refund_timeline")
    trace.emit(case_id=case_id, event_type="verification_completed", actor="verifier",
               decision_code="PASS" if not critical.intersection(failures)
               else "PARTIAL_EVIDENCE",
               evidence_refs=all_refs[:20], attributes={"failed_tools": len(failures)})
    return output
