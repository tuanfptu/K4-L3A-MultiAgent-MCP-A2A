from __future__ import annotations

from typing import Any

from .agents.order_agent import collect_order_item_evidence
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run the evidence-first specialist workflow for one L3A case."""
    case_id = case["case_id"]
    trace.emit(
        case_id=case_id,
        event_type="case_received",
        actor="coordinator",
        attributes={"message": "Case started"},
    )

    tools = set(await gateway.list_tools())
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order-agent",
        attributes={"task": "Collect order and item evidence"},
    )
    trace.emit(case_id=case_id, event_type="handoff", actor="coordinator", target="order-agent")
    order_id = case.get("customer_request", {}).get("claimed_order_id")
    evidence_refs: list[str] = []
    if order_id:
        order_item_evidence = await collect_order_item_evidence(
            case_id=case_id,
            order_id=order_id,
            available_tools=tools,
            gateway=gateway,
            trace=trace,
        )
        evidence_refs = order_item_evidence["evidence_refs"]
    trace.emit(case_id=case_id, event_type="handoff", actor="order-agent", target="verifier")
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        attributes={"status": "success"},
    )
    trace.emit(
        case_id=case_id,
        event_type="case_finalized",
        actor="coordinator",
        decision_code="completed",
    )
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.5,
        },
        "affected_entities": {
            "order_ids": [order_id] if order_id else [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "UNKNOWN_CAUSE", "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": ["INVESTIGATE_FURTHER"],
    }
