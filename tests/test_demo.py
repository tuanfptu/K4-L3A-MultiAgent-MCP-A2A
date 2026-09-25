from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from student_agent import demo
from student_agent.cases import CaseSet
from student_agent.contracts import Contracts


def sample_case() -> dict:
    return {
        "case_id": "CASE_001",
        "opened_at": "2018-01-12T09:00:00-03:00",
        "policy_version": "EC_POLICY_V1",
        "customer_request": {
            "claimed_order_id": "order-001",
            "claims": [{"claim_id": "claim-a", "topic": "canceled_order_paid"}],
        },
    }


def test_replay_reads_only_selected_case(tmp_path: Path, monkeypatch) -> None:
    case = sample_case()
    case_set = CaseSet("test-v1", "l3a", ("CASE_001",), {"CASE_001": case})
    monkeypatch.setattr(demo, "_case_set", lambda root: case_set)
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "CASE_001.json").write_text(
        json.dumps({"case_id": "CASE_001"}), encoding="utf-8"
    )
    (tmp_path / "traces").mkdir()
    (tmp_path / "traces" / "trace.jsonl").write_text(
        '{"case_id":"OTHER","event_type":"case_received"}\n'
        '{"case_id":"CASE_001","event_type":"case_received"}\n',
        encoding="utf-8",
    )
    result = demo.replay(tmp_path, "CASE_001")
    assert result["output"]["case_id"] == "CASE_001"
    assert len(result["events"]) == 1
    assert result["events"][0]["case_id"] == "CASE_001"


def test_live_demo_uses_new_trace_without_touching_submission(
    tmp_path: Path, monkeypatch
) -> None:
    case = sample_case()
    case_set = CaseSet("test-v1", "l3a", ("CASE_001",), {"CASE_001": case})
    monkeypatch.setattr(demo, "_case_set", lambda root: case_set)
    schema_root = Path(__file__).resolve().parents[1] / "contracts" / "schemas"
    monkeypatch.setattr(demo, "Contracts", lambda root: Contracts(schema_root))
    monkeypatch.setattr(
        demo.Settings, "load",
        lambda root: SimpleNamespace(mcp_endpoint="http://example", team_api_key="test"),
    )
    (tmp_path / "traces").mkdir()
    submission_trace = tmp_path / "traces" / "trace.jsonl"
    submission_trace.write_text("unchanged", encoding="utf-8")
    payloads = {
        "get_order": {
            "order_id": "order-001", "order_status": "canceled",
            "order_purchase_timestamp": "2018-01-01T09:00:00-03:00",
        },
        "get_order_items": [{
            "order_id": "order-001", "order_item_id": "item-001",
            "seller_id": "seller-001", "price": "79",
            "freight_value": "10",
            "shipping_limit_date": "2018-01-04T09:00:00-03:00",
        }],
        "get_payment_timeline": {
            "events": [{"event_at": "2018-01-01T10:00:00-03:00",
                        "event_type": "captured", "status": "confirmed",
                        "amount_brl": "79"}],
        },
        "get_shipment_summary": {"events": []},
        "get_policy": {"rules": {"canceled_order_paid": {
            "case_status": "action_required", "recommended_action": "issue_refund",
            "refund_brl": 79,
            "responsible_parties": [{"party_type": "platform", "party_id": None}],
        }}},
    }

    class Gateway:
        async def list_tools(self) -> list[str]:
            return list(payloads)

        async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict:
            assert case_id == "CASE_001"
            assert arguments
            return {
                "domain": "policy" if tool_name == "get_policy" else "order",
                "evidence_ref": "ev_" + tool_name.ljust(20, "_"),
                "data": payloads[tool_name],
            }

    @asynccontextmanager
    async def fake_connect(*args):
        yield Gateway()

    monkeypatch.setattr(demo, "connect_gateway", fake_connect)
    result = asyncio.run(demo._run_live(tmp_path, "CASE_001"))
    assert result["mode"] == "live"
    assert result["output"]["assessment"]["primary_issue"] == "canceled_order_paid"
    assert len(result["evidence"]) == 5
    assert result["events"][0]["event_type"] == "case_received"
    assert result["events"][-1]["event_type"] == "case_finalized"
    assert submission_trace.read_text(encoding="utf-8") == "unchanged"
