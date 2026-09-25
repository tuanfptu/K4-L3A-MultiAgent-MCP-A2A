from __future__ import annotations

import asyncio
from pathlib import Path

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import infer_issue, instant, solve_case

PURCHASE = "2018-01-01T09:00:00-03:00"
OPENED = "2018-01-12T09:00:00-03:00"
ORDER_ID = "order-test-001"


def test_future_and_pre_purchase_events_do_not_change_issue() -> None:
    order = {
        "order_id": ORDER_ID,
        "order_status": "delivered",
        "order_purchase_timestamp": PURCHASE,
        "order_delivered_customer_date": "2018-01-07T09:00:00-03:00",
        "order_estimated_delivery_date": "2018-01-08T09:00:00-03:00",
    }
    items = [{"price": "79.00", "freight_value": "10.00",
              "shipping_limit_date": "2018-01-04T09:00:00-03:00"}]
    payments = {"events": [
        {"event_at": "2018-01-01T10:00:00-03:00", "event_type": "captured",
         "status": "confirmed", "amount_brl": "89.00"},
        {"event_at": "2017-12-31T10:00:00-03:00", "event_type": "reconciliation_mismatch",
         "status": "open", "amount_brl": "35.00"},
        {"event_at": "2018-02-01T10:00:00-03:00", "event_type": "captured",
         "status": "confirmed", "amount_brl": "89.00"},
    ]}
    shipment = {"delivered_customer_at": "2018-01-07T09:00:00-03:00",
                "estimated_delivery_at": "2018-01-08T09:00:00-03:00",
                "events": [{"event_at": "2018-02-01T09:00:00-03:00",
                            "event_type": "delivered_late", "actor": "seller"}]}
    refunds = {"events": [{"event_at": "2018-02-02T09:00:00-03:00",
                           "event_type": "refund_requested", "status": "failed"}]}
    issue, _, _ = infer_issue(order, items, payments, shipment, refunds, instant(OPENED))
    assert issue == "unsupported_claim"


def test_split_payment_and_duplicate_charge_are_distinct() -> None:
    order = {"order_status": "delivered", "order_purchase_timestamp": PURCHASE}
    items = [{"price": "79.00", "freight_value": "10.00"}]
    shipment = {"estimated_delivery_at": "2018-02-01T09:00:00-03:00"}
    split_events = [
        {"event_at": "2018-01-01T10:00:00-03:00", "event_type": "captured",
         "status": "confirmed", "amount_brl": "44.50"},
        {"event_at": "2018-01-01T11:00:00-03:00", "event_type": "captured",
         "status": "confirmed", "amount_brl": "44.50"},
    ]
    issue, _, _ = infer_issue(order, items, {"events": split_events}, shipment, {},
                              instant(OPENED))
    assert issue == "valid_split_payment"
    duplicate_events = [dict(event, amount_brl="89.00") for event in split_events]
    issue, _, _ = infer_issue(order, items, {"events": duplicate_events}, shipment, {},
                              instant(OPENED))
    assert issue == "duplicate_charge"


def test_solver_links_only_consumed_evidence(tmp_path: Path) -> None:
    schema_root = Path(__file__).resolve().parents[1] / "contracts" / "schemas"
    contracts = Contracts(schema_root)
    case = {
        "case_id": "CASE_001", "opened_at": OPENED, "policy_version": "EC_POLICY_V1",
        "customer_request": {
            "claimed_order_id": ORDER_ID,
            "claims": [{"claim_id": "claim-a", "topic": "canceled_order_paid"}],
        },
    }
    data = {
        "get_order": {"order_id": ORDER_ID, "order_status": "canceled",
                      "order_purchase_timestamp": PURCHASE},
        "get_order_items": [{"order_id": ORDER_ID, "order_item_id": "item-1",
                            "seller_id": "seller-1", "price": "79.00",
                            "freight_value": "10.00",
                            "shipping_limit_date": "2018-01-04T09:00:00-03:00"}],
        "get_payment_timeline": {"events": [
            {"event_at": "2018-01-01T10:00:00-03:00", "event_type": "captured",
             "status": "confirmed", "amount_brl": "79.00"}]},
        "get_shipment_summary": {"events": []},
        "get_policy": {"rules": {"canceled_order_paid": {
            "case_status": "action_required", "recommended_action": "issue_refund",
            "refund_brl": 79.0,
            "responsible_parties": [{"party_type": "platform", "party_id": None}],
        }}},
    }

    class Gateway:
        async def list_tools(self) -> list[str]:
            return list(data)

        async def call(self, tool: str, *, case_id: str, **arguments: str) -> dict:
            assert case_id == "CASE_001"
            assert arguments
            return {"evidence_ref": "ev_" + tool.ljust(20, "_"), "data": data[tool]}

    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    output = asyncio.run(solve_case(case, Gateway(), trace))
    contracts.validate_output(output, "test output")
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["financial_resolution"]["recommended_refund_brl"] == 79.0
    assert len(output["evidence_refs"]) == 4
    assert all(ref.startswith("ev_") for ref in output["evidence_refs"])
