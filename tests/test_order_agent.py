from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from student_agent.agents.order_agent import collect_order_item_evidence
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def call(self, tool_name: str, *, case_id: str, order_id: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, order_id))
        suffix = "order" if tool_name == "get_order" else "items"
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{suffix}_12345678901234567890",
            "result_hash": "sha256:" + "a" * 64,
            "domain": "order" if suffix == "order" else "item",
            "data": {"source": suffix},
        }


def test_order_agent_collects_order_and_items_with_trace(tmp_path: Path) -> None:
    gateway = FakeGateway()
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    result = asyncio.run(
        collect_order_item_evidence(
            case_id="L3A_CASE_001",
            order_id="e2a03ccf5ea816036608b2d8c3ab8e60",
            available_tools={"get_order", "get_order_items"},
            gateway=gateway,  # type: ignore[arg-type]
            trace=trace,
        )
    )

    assert gateway.calls == [
        ("get_order", "L3A_CASE_001", "e2a03ccf5ea816036608b2d8c3ab8e60"),
        ("get_order_items", "L3A_CASE_001", "e2a03ccf5ea816036608b2d8c3ab8e60"),
    ]
    assert result["order"] == {"source": "order"}
    assert result["items"] == {"source": "items"}
    assert result["evidence_refs"] == [
        "ev_order_12345678901234567890",
        "ev_items_12345678901234567890",
    ]

    events = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
    assert [event["tool_name"] for event in events] == ["get_order", "get_order_items"]
    assert [event["evidence_refs"] for event in events] == [
        ["ev_order_12345678901234567890"],
        ["ev_items_12345678901234567890"],
    ]


def test_order_agent_does_not_call_undiscovered_tools(tmp_path: Path) -> None:
    gateway = FakeGateway()
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    result = asyncio.run(
        collect_order_item_evidence(
            case_id="L3A_CASE_001",
            order_id="e2a03ccf5ea816036608b2d8c3ab8e60",
            available_tools={"get_order"},
            gateway=gateway,  # type: ignore[arg-type]
            trace=trace,
        )
    )

    assert gateway.calls == [
        ("get_order", "L3A_CASE_001", "e2a03ccf5ea816036608b2d8c3ab8e60")
    ]
    assert result["items"] is None