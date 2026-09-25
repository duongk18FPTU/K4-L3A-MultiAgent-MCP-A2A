from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


class FakeGateway:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def list_tools(self) -> list[str]:
        return sorted(self.responses)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        return {
            "evidence_ref": f"ev_{tool_name.replace('_', ''):0<24}",
            "data": self.responses[tool_name],
        }


def test_canceled_order_workflow_uses_scoped_evidence_and_valid_contract(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    case = {
        "case_id": "L3A_CASE_TEST",
        "customer_request": {
            "claimed_order_id": "order-1",
            "claims": [
                {"claim_id": "claim-a", "topic": "canceled_order_paid"},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V1",
    }
    gateway = FakeGateway(
        {
            "get_order": {
                "order_id": "order-1",
                "order_status": "canceled",
                "order_approved_at": "2018-01-01T10:00:00-03:00",
                "order_delivered_customer_date": None,
                "order_estimated_delivery_date": "2018-01-10T10:00:00-03:00",
            },
            "get_order_items": [
                {
                    "order_id": "order-1",
                    "order_item_id": "item-1",
                    "seller_id": "seller-1",
                    "shipping_limit_date": "2018-01-03T10:00:00-03:00",
                    "price": "69.00",
                    "freight_value": "10.00",
                }
            ],
            "get_payment_timeline": {
                "order_id": "order-1",
                "events": [
                    {
                        "event_at": "2018-01-01T10:05:00-03:00",
                        "event_type": "captured",
                        "amount_brl": "79.00",
                        "status": "confirmed",
                    }
                ],
            },
            "get_policy": {
                "policy_version": "EC_POLICY_V1",
                "rules": {
                    "canceled_order_paid": {
                        "case_status": "action_required",
                        "recommended_action": "issue_refund",
                        "refund_brl": 79.0,
                        "responsible_parties": [{"party_type": "platform", "party_id": None}],
                    }
                },
            },
        }
    )
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)

    output = asyncio.run(solve_case(case, gateway, trace))

    contracts.validate_output(output, "test output")
    assert output["assessment"] == {
        "primary_issue": "canceled_order_paid",
        "case_status": "action_required",
        "confidence": 0.96,
    }
    assert output["financial_resolution"]["recommended_refund_brl"] == 79.0
    assert all(call_case_id == case["case_id"] for _, call_case_id, _ in gateway.calls)

    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    consumed = [event for event in events if event["event_type"] == "tool_result_consumed"]
    assert len(consumed) == 4
    assert all(event["case_id"] == case["case_id"] for event in events)
    assert set(output["evidence_refs"]) == {event["evidence_refs"][0] for event in consumed}
