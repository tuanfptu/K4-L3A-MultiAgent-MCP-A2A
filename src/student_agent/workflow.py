from __future__ import annotations

import os
import json
from typing import Any

from google import genai
from pydantic import BaseModel, Field

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Implement the L3A coordinator and specialist-agent workflow here.
    """
    case_id = case["case_id"]
    
    # 1. Trace: Case received
    trace.emit(
        case_id=case_id,
        event_type="case_received",
        actor="coordinator",
        payload={"message": "Case started"}
    )
    
    # 2. Get available tools
    tools = await gateway.list_tools()
    
    # 3. TODO: Initialize Gemini client
    # client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    
    # 4. Handoff to specialists
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        specialist="order-agent",
        task="Investigate order evidence"
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        source="coordinator",
        destination="order-agent"
    )
    
    # 5. Example tool call
    order_id = case.get("customer_request", {}).get("claimed_order_id")
    evidence_refs = []
    if order_id and "get_order" in tools:
        evidence = await gateway.call("get_order", case_id=case_id, order_id=order_id)
        evidence_ref = evidence["evidence_ref"]
        evidence_refs.append(evidence_ref)
        
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="order-agent",
            tool_name="get_order",
            evidence_refs=[evidence_ref]
        )
    
    # 6. Handoff to Verifier
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        source="order-agent",
        destination="verifier"
    )
    
    # 7. Verification completed
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        status="success"
    )
    
    # 8. Finalized
    trace.emit(
        case_id=case_id,
        event_type="case_finalized",
        actor="coordinator",
        result="completed"
    )
    
    # Return placeholder compliant format
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.5
        },
        "affected_entities": {
            "order_ids": [order_id] if order_id else [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": []
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "UNKNOWN_CAUSE", "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}]
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": []
        },
        "resolution_actions": ["INVESTIGATE_FURTHER"]
    }
