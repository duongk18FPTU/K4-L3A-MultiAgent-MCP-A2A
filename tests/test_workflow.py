"""Synthetic gateway fixtures only; these refs never enter submission artifacts."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import TOOLS, CaseWorkflow, EvidenceError, money, solve_case

ROOT = Path(__file__).resolve().parents[1]
RULES = {
    "canceled_order_paid": ("action_required", "issue_refund", 89, "platform"),
    "unavailable_order_paid": ("action_required", "issue_refund", 89, "seller"),
    "late_delivery_seller": ("action_required", "refund_freight", 10, "seller"),
    "late_delivery_logistics": ("action_required", "refund_freight", 10, "logistics_provider"),
    "payment_mismatch": ("action_required", "reconcile_payment", 35, "payment_provider"),
    "duplicate_charge": ("action_required", "refund_duplicate_charge", 89, "payment_provider"),
    "valid_split_payment": ("no_action", "document_no_action", 0, "customer"),
    "refund_pending": ("needs_investigation", "monitor_refund", 0, "payment_provider"),
    "refund_failed": ("action_required", "retry_refund", 89, "payment_provider"),
    "unsupported_claim": ("no_action", "document_no_action", 0, "customer"),
}


def fixture(issue="unsupported_claim"):
    case = {
        "case_id": "TEST_CASE_001",
        "opened_at": "2018-01-20T00:00:00Z",
        "policy_version": "TEST_POLICY",
        "customer_request": {
            "claimed_order_id": "order-1",
            "claims": [
                {"claim_id": "a", "topic": issue},
                {"claim_id": "b", "topic": "requested_full_refund"},
            ],
        },
    }
    order = {
        "order_id": "order-1",
        "order_status": "delivered",
        "order_purchase_timestamp": "2018-01-01T00:00:00Z",
        "order_delivered_carrier_date": "2018-01-02T00:00:00Z",
        "order_delivered_customer_date": "2018-01-09T00:00:00Z",
        "order_estimated_delivery_date": "2018-01-10T00:00:00Z",
    }
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        order["order_status"] = issue.split("_")[0]
        order["order_delivered_customer_date"] = None
    if issue.startswith("late_delivery"):
        order["order_delivered_customer_date"] = "2018-01-12T00:00:00Z"
        if issue.endswith("seller"):
            order["order_delivered_carrier_date"] = "2018-01-05T00:00:00Z"
    items = [
        {
            "order_id": "order-1",
            "order_item_id": "item-1",
            "seller_id": "seller-1",
            "shipping_limit_date": "2018-01-03T00:00:00Z",
            "price": "79.00",
            "freight_value": "10.00",
        }
    ]
    values = {
        "valid_split_payment": ["44.50", "44.50"],
        "duplicate_charge": ["89.00", "89.00"],
        "payment_mismatch": ["35.00"],
    }
    payments = [
        {
            "order_id": "order-1",
            "payment_sequential": str(i),
            "payment_reference": f"payment-{i}",
            "payment_value": value,
        }
        for i, value in enumerate(values.get(issue, ["89.00"]), 1)
    ]
    events = [
        {
            "order_id": "order-1",
            "event_type": "captured",
            "status": "confirmed",
            "event_at": f"2018-01-01T0{i}:00:00Z",
            "amount_brl": row["payment_value"],
        }
        for i, row in enumerate(payments, 1)
    ]
    shipment = {
        "order_id": "order-1",
        "order_status": order["order_status"],
        "delivered_carrier_at": order["order_delivered_carrier_date"],
        "delivered_customer_at": order["order_delivered_customer_date"],
        "estimated_delivery_at": order["order_estimated_delivery_date"],
        "events": [],
    }
    refunds = []
    if issue.startswith("refund_"):
        refunds = [
            {
                "order_id": "order-1",
                "event_type": "refund_requested",
                "event_at": "2018-01-15T00:00:00Z",
                "amount_brl": "89.00",
                "status": issue.removeprefix("refund_"),
            }
        ]
    data = {
        "get_order": order,
        "get_sellers": [{"seller_id": "seller-1"}],
        "get_order_items": items,
        "get_order_payments": payments,
        "get_payment_timeline": {"order_id": "order-1", "payments": payments, "events": events},
        "get_shipment_summary": shipment,
        "get_refund_timeline": {"order_id": "order-1", "events": refunds},
        "get_policy": {
            "policy_version": "TEST_POLICY",
            "currency": "BRL",
            "rules": {
                name: {
                    "case_status": status,
                    "recommended_action": action,
                    "refund_brl": amount,
                    "responsible_parties": [{"party_type": role, "party_id": None}],
                }
                for name, (status, action, amount, role) in RULES.items()
            },
        },
    }
    return case, data


class FakeGateway:
    def __init__(self, case, data):
        self.case = case
        self.data = data
        self.calls = []
        self.active = self.peak = 0
        self.error = None
        self.discovered = False

    async def describe_tools(self):
        self.discovered = True
        return {
            name: {
                "inputSchema": {
                    "type": "object",
                    "required": ["case_id", key],
                    "additionalProperties": False,
                    "properties": {"case_id": {"type": "string"}, key: {"type": "string"}},
                }
            }
            for name in TOOLS
            for key in ["policy_version" if name == "get_policy" else "order_id"]
        }

    async def call(self, name, **arguments):
        assert self.discovered
        assert arguments["case_id"] == self.case["case_id"]
        if name != "get_policy":
            assert arguments["order_id"] == self.case["customer_request"]["claimed_order_id"]
        self.calls.append((name, arguments))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0)
            if self.error:
                raise self.error
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": f"ev_UNIT_TEST_ONLY_{list(TOOLS).index(name):020d}",
                "result_hash": "sha256:" + "0" * 64,
                "domain": TOOLS[name][1],
                "data": copy.deepcopy(self.data[name]),
                "warnings": [],
            }
        finally:
            self.active -= 1


def run(tmp_path, case, data):
    gateway = FakeGateway(case, data)
    trace = TraceWriter(tmp_path / "trace.jsonl", Contracts(ROOT / "contracts/schemas"))
    output = asyncio.run(solve_case(case, gateway, trace))
    events = [json.loads(line) for line in trace.path.read_text().splitlines()]
    return output, events, gateway


@pytest.mark.parametrize("issue", RULES)
def test_issue_policy_and_lifecycle(tmp_path, issue):
    case, data = fixture(issue)
    original = copy.deepcopy(case)
    output, events, gateway = run(tmp_path, case, data)
    assert case == original
    assert output["assessment"]["primary_issue"] == issue
    status, action, amount, party = RULES[issue]
    assert output["assessment"]["case_status"] == status
    assert output["resolution_actions"] == [action]
    assert output["financial_resolution"]["recommended_refund_brl"] == amount
    assert output["root_cause_analysis"]["responsible_parties"][0]["party_type"] == party
    if party == "seller":
        assert output["root_cause_analysis"]["responsible_parties"][0]["party_id"] == "seller-1"
    kinds = [event["event_type"] for event in events]
    assert kinds[0] == "case_received" and kinds[-1] == "case_finalized"
    assert kinds.count("case_received") == kinds.count("case_finalized") == 1
    assert kinds.index("policy_decided") < kinds.index("verification_completed")
    consumed = {
        ref
        for event in events
        if event["event_type"] == "tool_result_consumed"
        for ref in event["evidence_refs"]
    }
    assert set(output["evidence_refs"]) <= consumed
    assert gateway.peak == 3
    assert gateway.active == 0


def test_customer_claim_is_not_ground_truth(tmp_path):
    case, data = fixture()
    case["customer_request"]["claims"][0]["topic"] = "duplicate_charge"
    output, _, _ = run(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["claim_assessments"][0]["verdict"] == "unsupported"


def test_conflict_prevents_blame_and_refund(tmp_path):
    case, data = fixture("late_delivery_seller")
    data["get_shipment_summary"]["order_status"] = "canceled"
    output, _, _ = run(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["confidence"] <= 0.5
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert output["root_cause_analysis"]["responsible_parties"] == []


def test_out_of_window_events_do_not_create_duplicate(tmp_path):
    case, data = fixture("valid_split_payment")
    event = copy.deepcopy(data["get_payment_timeline"]["events"][0])
    event["event_at"] = "2019-01-01T00:00:00Z"
    data["get_payment_timeline"]["events"].append(event)
    output, _, _ = run(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "valid_split_payment"
    assert output["assessment"]["confidence"] < 0.95
    assert output["data_conflicts"]


def test_latest_refund_completion_supersedes_failure(tmp_path):
    case, data = fixture("refund_failed")
    data["get_refund_timeline"]["events"].append(
        {
            "order_id": "order-1",
            "event_at": "2018-01-16T00:00:00Z",
            "event_type": "refund_completed",
            "status": "completed",
            "amount_brl": "89.00",
        }
    )
    output, _, _ = run(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0


@pytest.mark.parametrize("problem", ["scope", "forbidden", "missing_tool", "timeout"])
def test_invalid_collection_never_finalizes(tmp_path, problem):
    case, data = fixture()
    gateway = FakeGateway(case, data)
    trace = TraceWriter(tmp_path / "trace.jsonl", Contracts(ROOT / "contracts/schemas"))
    if problem == "scope":
        data["get_order"]["order_id"] = "other-order"
    elif problem == "forbidden":
        gateway.error = RuntimeError("403 Forbidden")
    elif problem == "timeout":
        gateway.error = TimeoutError()
    else:

        async def empty():
            gateway.discovered = True
            return {}

        gateway.describe_tools = empty
    with pytest.raises(EvidenceError):
        asyncio.run(solve_case(case, gateway, trace))
    assert gateway.active == 0
    assert '"event_type":"case_finalized"' not in trace.path.read_text()


def test_missing_capture_is_not_zero_payment(tmp_path):
    case, data = fixture("canceled_order_paid")
    data["get_payment_timeline"]["events"] = []
    output, _, _ = run(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"


def test_verifier_rejects_mutated_reference(tmp_path):
    case, data = fixture()
    trace = TraceWriter(tmp_path / "trace.jsonl", Contracts(ROOT / "contracts/schemas"))
    workflow = CaseWorkflow(case, FakeGateway(case, data), trace)
    output = asyncio.run(workflow.run())
    output["evidence_refs"] = ["ev_UNIT_TEST_FOREIGN_SCOPE_000000"]
    with pytest.raises(EvidenceError, match="unconsumed"):
        workflow.verify()


@pytest.mark.parametrize("value", [None, True, -1, "NaN", "Infinity", "1.001", "bad"])
def test_money_rejects_invalid_values(value):
    with pytest.raises(EvidenceError):
        money(value)


def test_policy_version_mismatch_fails(tmp_path):
    case, data = fixture()
    data["get_policy"]["policy_version"] = "OTHER"
    with pytest.raises(EvidenceError, match="Policy version"):
        run(tmp_path, case, data)


def test_delivery_after_opening_can_resolve_late_delivery(tmp_path):
    case, data = fixture("late_delivery_seller")
    case["opened_at"] = "2018-01-11T00:00:00Z"
    output, _, _ = run(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "late_delivery_seller"


def test_refund_cannot_exceed_captured_money(tmp_path):
    case, data = fixture("canceled_order_paid")
    data["get_policy"]["rules"]["canceled_order_paid"]["refund_brl"] = 900
    output, _, _ = run(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0


def test_seller_must_exist_in_authoritative_seller_records(tmp_path):
    case, data = fixture("late_delivery_seller")
    data["get_sellers"] = [{"seller_id": "seller-other"}]
    output, _, _ = run(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"


def test_two_cases_have_independent_scopes(tmp_path):
    async def solve_both():
        first, first_data = fixture("canceled_order_paid")
        second, second_data = fixture("valid_split_payment")
        second["case_id"] = "TEST_CASE_002"
        trace = TraceWriter(tmp_path / "trace.jsonl", Contracts(ROOT / "contracts/schemas"))
        return await asyncio.gather(
            solve_case(first, FakeGateway(first, first_data), trace),
            solve_case(second, FakeGateway(second, second_data), trace),
        )

    outputs = asyncio.run(solve_both())
    assert [output["case_id"] for output in outputs] == ["TEST_CASE_001", "TEST_CASE_002"]
    assert [output["assessment"]["primary_issue"] for output in outputs] == [
        "canceled_order_paid",
        "valid_split_payment",
    ]
