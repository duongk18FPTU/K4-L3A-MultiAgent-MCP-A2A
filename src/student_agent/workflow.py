"""Evidence-driven, deterministic specialists for the public L3A contract."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

# These are discovered public tool names, not inferred endpoints.
OWNERSHIP = {
    "order-agent": {"get_order", "get_order_items"},
    "payment-agent": {"get_payment_timeline", "get_refund_timeline"},
    "shipment-agent": {"get_shipment_summary"},
    "policy-agent": {"get_policy"},
}
DOMAINS = {
    "get_order": "order",
    "get_order_items": "item",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment",
    "get_policy": "policy",
}


def money(value: Any) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("Invalid monetary amount")
    return amount.quantize(Decimal("0.01"))


def timestamp(value: Any) -> datetime:
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return result


def ids(rows: list[dict], key: str) -> list[str]:
    return sorted({str(row[key]) for row in rows if row.get(key) is not None})


def check_scope(data: Any, order_id: str) -> None:
    """Reject cross-order records recursively, including nested timeline rows."""
    if isinstance(data, dict):
        if "order_id" in data and data["order_id"] != order_id:
            raise ValueError("Cross-order evidence")
        for value in data.values():
            check_scope(value, order_id)
    elif isinstance(data, list):
        for value in data:
            check_scope(value, order_id)


@dataclass
class Investigation:
    case: dict
    gateway: EvidenceGateway
    trace: TraceWriter
    available: set[str]
    evidence: dict[str, dict] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    conflicts: list[dict] = field(default_factory=list)

    @property
    def case_id(self) -> str:
        return self.case["case_id"]

    @property
    def order_id(self) -> str:
        return self.case["customer_request"]["claimed_order_id"]

    def emit(self, event: str, actor: str, **kwargs: Any) -> None:
        self.trace.emit(case_id=self.case_id, event_type=event, actor=actor, **kwargs)

    def refs(self, *tools: str) -> list[str]:
        return list(
            dict.fromkeys(self.evidence[t]["evidence_ref"] for t in tools if t in self.evidence)
        )

    def conflict(self, name: str, selected: str | None, code: str) -> None:
        entry = {
            "field": name,
            "sources": ["authoritative_order", "mcp_records"],
            "selected_source": selected,
            "resolution_code": code,
        }
        if entry not in self.conflicts:
            self.conflicts.append(entry)

    async def collect(self, actor: str, tools: list[str]) -> None:
        self.emit(
            "task_assigned", "coordinator", target=actor, decision_code="COLLECT_SCOPED_EVIDENCE"
        )
        for tool in tools:
            if tool not in OWNERSHIP[actor]:
                raise ValueError("Actor attempted an unauthorized tool")
            if tool not in self.available:
                self.failures[tool] = "TOOL_UNAVAILABLE"
                continue
            args = (
                {"policy_version": self.case["policy_version"]}
                if tool == "get_policy"
                else {"order_id": self.order_id}
            )
            for attempt in range(2):
                try:
                    async with asyncio.timeout(30):
                        evidence = await self.gateway.call(tool, case_id=self.case_id, **args)
                    self.trace.contracts.validate_evidence(evidence)
                    if evidence["domain"] != DOMAINS[tool]:
                        raise ValueError("Unexpected evidence domain")
                    if tool != "get_policy":
                        check_scope(evidence["data"], self.order_id)
                    self.evidence[tool] = evidence
                    self.emit(
                        "tool_result_consumed",
                        actor,
                        tool_name=tool,
                        evidence_refs=[evidence["evidence_ref"]],
                    )
                    break
                except (TimeoutError, ConnectionError):
                    self.failures[tool] = "MCP_TIMEOUT"
                    if attempt == 0:
                        await asyncio.sleep(0.25)
                        continue
                except Exception as exc:
                    self.failures[tool] = type(exc).__name__
                break
            if tool in self.evidence:
                self.failures.pop(tool, None)
        self.emit(
            "handoff",
            actor,
            target="coordinator",
            evidence_refs=self.refs(*tools),
            decision_code="EVIDENCE_READY"
            if all(t in self.evidence for t in tools)
            else "EVIDENCE_INCOMPLETE",
        )

    def data(self, tool: str) -> Any:
        if tool not in self.evidence:
            raise ValueError("Required evidence unavailable")
        return self.evidence[tool]["data"]

    def events(self, tool: str, start: datetime, end: datetime) -> list[dict]:
        rows = self.data(tool)["events"]
        result = []
        for row in rows:
            when = timestamp(row["event_at"])
            if start <= when <= end:
                result.append(row)
            else:
                self.conflict(f"{tool}.event_at", "authoritative_order", "OUTSIDE_CASE_WINDOW")
        return sorted(result, key=lambda row: timestamp(row["event_at"]))


def order_specialist(ctx: Investigation) -> tuple[dict, list[dict], datetime, datetime]:
    order = ctx.data("get_order")
    if order["order_id"] != ctx.order_id:
        raise ValueError("Order identity mismatch")
    start, end = timestamp(order["order_purchase_timestamp"]), timestamp(ctx.case["opened_at"])
    if start > end:
        ctx.conflict("order_purchase_timestamp", None, "ORDER_AFTER_CASE_OPENED")
        raise ValueError("Order chronology cannot support this case")
    groups: dict[str, list[dict]] = {}
    for row in ctx.data("get_order_items"):
        groups.setdefault(str(row["order_item_id"]), []).append(row)
    items = []
    for rows in groups.values():
        # Exact duplicates are one item, not two purchases.
        unique = {tuple(sorted(row.items())): row for row in rows}
        rows = list(unique.values())
        if len(rows) > 1:
            current = [r for r in rows if start <= timestamp(r["shipping_limit_date"]) <= end]
            ctx.conflict(
                "items.shipping_limit_date",
                "authoritative_order" if len(current) == 1 else None,
                "ITEM_VERSION_CONFLICT",
            )
            if len(current) != 1:
                raise ValueError("Ambiguous item versions")
            rows = current
        items.append(rows[0])
    if not items:
        raise ValueError("No item evidence")
    return order, items, start, end


def payment_specialist(
    ctx: Investigation, start: datetime, end: datetime, items: list[dict]
) -> dict:
    events = ctx.events("get_payment_timeline", start, end)
    captures = [
        e
        for e in events
        if e["event_type"] == "captured"
        and e.get("status") in {"confirmed", "completed", "success"}
    ]
    paid = sum((money(e["amount_brl"]) for e in captures), Decimal("0"))
    expected = sum((money(i["price"]) + money(i["freight_value"]) for i in items), Decimal("0"))
    issue = None
    if any(
        e["event_type"] == "reconciliation_mismatch" and e.get("status") == "open" for e in events
    ):
        issue = "payment_mismatch"
    elif any(
        e["event_type"] == "duplicate_charge" and e.get("status") == "confirmed" for e in events
    ):
        issue = "duplicate_charge"
    elif len(captures) > 1 and paid == expected:
        issue = "valid_split_payment"
    elif paid and paid != expected:
        issue = "payment_mismatch"
    refunds = (
        ctx.events("get_refund_timeline", start, end)
        if "get_refund_timeline" in ctx.evidence
        else None
    )
    if refunds:
        latest = refunds[-1]
        if latest.get("status") == "failed":
            issue = "refund_failed"
        elif latest.get("status") in {"pending", "requested", "processing"}:
            issue = "refund_pending"
    return {
        "issue": issue,
        "paid": paid,
        "expected": expected,
        "refunds": refunds,
        "events": events,
    }


def shipment_specialist(
    ctx: Investigation, order: dict, items: list[dict], start: datetime, end: datetime
) -> tuple[str | None, list[str]]:
    shipment = ctx.data("get_shipment_summary")
    for source, target in [
        ("delivered_customer_at", "order_delivered_customer_date"),
        ("delivered_carrier_at", "order_delivered_carrier_date"),
        ("estimated_delivery_at", "order_estimated_delivery_date"),
    ]:
        if shipment.get(source) != order.get(target):
            ctx.conflict(source, None, "ORDER_SHIPMENT_DISAGREE")
            raise ValueError("Conflicting shipment timestamps")
    due = timestamp(shipment["estimated_delivery_at"])
    delivered = shipment.get("delivered_customer_at")
    actual = timestamp(delivered) if delivered else end
    if actual > end and delivered:
        raise ValueError("Delivery is after case opened")
    if actual <= due:
        return None, []
    handoff = shipment.get("delivered_carrier_at")
    late_sellers = sorted(
        {
            str(i["seller_id"])
            for i in items
            if (timestamp(handoff) if handoff else end) > timestamp(i["shipping_limit_date"])
        }
    )
    events = ctx.events("get_shipment_summary", start, end)
    actors = {
        e.get("actor")
        for e in events
        if e.get("event_type") == "delivered_late" and e.get("status") == "confirmed"
    }
    issue = "late_delivery_seller" if late_sellers else "late_delivery_logistics"
    if ("seller" in actors and not late_sellers) or (
        "logistics_provider" in actors and late_sellers
    ):
        ctx.conflict("shipment.responsibility", None, "TIMELINE_ACTOR_CONFLICT")
        raise ValueError("Conflicting responsibility")
    return issue, late_sellers


def policy_specialist(
    ctx: Investigation, issue: str, paid: Decimal, items: list[dict], sellers: list[str]
) -> tuple[dict, Decimal, list[dict]]:
    policy = ctx.data("get_policy")
    if policy["policy_version"] != ctx.case["policy_version"] or policy["currency"] != "BRL":
        raise ValueError("Policy mismatch")
    rule = policy["rules"][issue]
    amount = money(rule["refund_brl"])
    if amount > paid:
        raise ValueError("Policy refund exceeds evidenced captured payment")
    parties = []
    for party in rule["responsible_parties"]:
        if party["party_type"] == "seller":
            # Policy templates may contain a seller from another order. Never copy that ID.
            eligible = sellers if issue == "late_delivery_seller" else ids(items, "seller_id")
            parties.extend({"party_type": "seller", "party_id": s} for s in eligible)
        elif party.get("party_id") is not None:
            raise ValueError("Party ID is not established by scoped evidence")
        else:
            parties.append(dict(party))
    ctx.emit(
        "policy_decided",
        "policy-agent",
        decision_code=issue.upper(),
        evidence_refs=ctx.refs("get_policy"),
    )
    return rule, amount, parties


def verify(ctx: Investigation, output: dict, items: list[dict]) -> None:
    ctx.trace.contracts.validate_output(output, "workflow output")
    refs = set(ctx.refs(*ctx.evidence))
    if output["case_id"] != ctx.case_id or not set(output["evidence_refs"]) <= refs:
        raise ValueError("Evidence ownership mismatch")
    for claim in output.get("claim_assessments", []):
        if not set(claim["evidence_refs"]) <= set(output["evidence_refs"]):
            raise ValueError("Claim evidence missing from output")
    financial = output["financial_resolution"]
    if sum((money(r["amount_brl"]) for r in financial["refund_lines"]), Decimal("0")) != money(
        financial["recommended_refund_brl"]
    ):
        raise ValueError("Refund line sum mismatch")
    known_sellers = set(ids(items, "seller_id"))
    for party in output["root_cause_analysis"]["responsible_parties"]:
        if party["party_type"] == "seller" and party["party_id"] not in known_sellers:
            raise ValueError("Unverified seller")
    if output["assessment"]["case_status"] == "no_action" and money(
        financial["recommended_refund_brl"]
    ):
        raise ValueError("No-action case cannot recommend refund")
    ctx.emit(
        "verification_completed",
        "verifier",
        decision_code="SCHEMA_SCOPE_MONEY_CHECKED",
        evidence_refs=output["evidence_refs"],
        attributes={"valid": True},
    )


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Investigate one case. CLI owns case_received/case_finalized lifecycle events."""
    ctx = Investigation(case, gateway, trace, set(await gateway.list_tools()))
    # Four bounded specialists; each performs its own tools sequentially.
    await asyncio.gather(*(ctx.collect(actor, sorted(tools)) for actor, tools in OWNERSHIP.items()))
    items: list[dict] = []
    issue, status, confidence = "insufficient_evidence", "needs_investigation", 0.2
    amount, paid = Decimal("0"), Decimal("0")
    parties: list[dict] = []
    actions = ["request_manual_review"]
    selected: list[str] = sorted(ctx.evidence)
    try:
        order, items, start, end = order_specialist(ctx)
        payment = payment_specialist(ctx, start, end, items)
        paid = payment["paid"]
        shipping_issue, sellers = shipment_specialist(ctx, order, items, start, end)
        if payment["issue"] in {"refund_failed", "refund_pending"}:
            issue = payment["issue"]
        elif order["order_status"] in {"canceled", "unavailable"} and paid > 0:
            issue = f"{order['order_status']}_order_paid"
        elif shipping_issue:
            issue = shipping_issue
        elif payment["issue"]:
            issue = payment["issue"]
        elif paid and order["order_status"] == "delivered":
            issue = "unsupported_claim"
        else:
            raise ValueError("Evidence does not support a known issue")
        # A failed refund lookup is not proof there is no refund. Do not issue a second refund.
        if payment["refunds"] is None:
            raise ValueError("Refund lifecycle unavailable")
        rule, amount, parties = policy_specialist(ctx, issue, paid, items, sellers)
        # Completed refunds need an explicit balance policy before another payout.
        if amount and any(
            r.get("status") in {"completed", "success", "confirmed"} for r in payment["refunds"]
        ):
            raise ValueError("Existing refund requires balance reconciliation")
        status, actions = rule["case_status"], [rule["recommended_action"]]
        if status not in {"action_required", "no_action", "needs_investigation"}:
            raise ValueError("Invalid policy status")
        if not isinstance(actions[0], str) or not 1 <= len(actions[0]) <= 80:
            raise ValueError("Invalid policy action")
        if status == "no_action" and amount:
            raise ValueError("Contradictory policy")
        confidence = 0.75 if ctx.conflicts else 0.9
    except (ValueError, KeyError, TypeError, InvalidOperation):
        issue, status, confidence = "insufficient_evidence", "needs_investigation", 0.2
        amount, parties, actions = Decimal("0"), [], ["request_manual_review"]
    references = ctx.refs(*selected)
    entities = {
        "order_ids": [ctx.order_id] if "get_order" in ctx.evidence else [],
        "item_ids": ids(items, "order_item_id"),
        "seller_ids": ids(items, "seller_id"),
        "payment_references": [],
        "shipment_ids": [],
    }
    claims = []
    for claim in case["customer_request"].get("claims", []):
        topic = claim["topic"]
        if issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        elif topic == "requested_full_refund":
            verdict = (
                "supported"
                if amount > 0 and amount == paid
                else "partially_supported"
                if amount > 0
                else "unsupported"
            )
        elif topic == issue:
            verdict = "supported"
        else:
            # Another primary issue need not disprove this particular claim.
            verdict = "insufficient_evidence"
        claims.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence if verdict != "insufficient_evidence" else 0.2,
                "evidence_refs": references,
            }
        )
    output = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": ctx.case_id,
        "assessment": {"primary_issue": issue, "case_status": status, "confidence": confidence},
        "affected_entities": entities,
        "claim_assessments": claims,
        "root_cause_analysis": {
            "ranked_causes": []
            if issue == "insufficient_evidence"
            else [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": parties,
        },
        "evidence_refs": references,
        "data_conflicts": ctx.conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(amount),
            "refund_lines": [
                {
                    "reason_code": issue.upper(),
                    "amount_brl": float(amount),
                    "entity_id": ctx.order_id,
                }
            ]
            if amount
            else [],
        },
        "resolution_actions": actions,
    }
    ctx.emit(
        "handoff",
        "coordinator",
        target="verifier",
        evidence_refs=references,
        decision_code="VERIFY_PROPOSED_RESOLUTION",
    )
    verify(ctx, output, items)
    return output
