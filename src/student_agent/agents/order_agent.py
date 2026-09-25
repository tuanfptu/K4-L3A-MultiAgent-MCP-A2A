from __future__ import annotations

from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter


async def collect_order_item_evidence(
    *,
    case_id: str,
    order_id: str,
    available_tools: set[str],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    include_sellers: bool = False,
) -> dict[str, Any]:
    """Collect order and item evidence without making claims about the case."""
    evidence: dict[str, Any] = {
        "order": None,
        "items": None,
        "sellers": None,
        "evidence_refs": [],
        "refs_by_domain": {},
    }

    tool_requests = [("get_order", "order"), ("get_order_items", "items")]
    if include_sellers:
        tool_requests.append(("get_sellers", "sellers"))
    for tool_name, result_key in tool_requests:
        if tool_name not in available_tools:
            continue
        response = await gateway.call(tool_name, case_id=case_id, order_id=order_id)
        evidence[result_key] = response["data"]
        evidence_ref = response["evidence_ref"]
        evidence["evidence_refs"].append(evidence_ref)
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="order-agent",
            tool_name=tool_name,
            evidence_refs=[evidence_ref],
        )
        evidence["refs_by_domain"][result_key] = [evidence_ref]

    return evidence