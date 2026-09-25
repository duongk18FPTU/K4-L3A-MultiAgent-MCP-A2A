from __future__ import annotations

import asyncio
import copy
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum, auto
from typing import Any

from jsonschema import Draft202012Validator

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

# Adapters for discovered public tools. Customer text cannot select arbitrary tools.
TOOLS = {
    "get_order": ("order-agent", "order"),
    "get_order_items": ("order-agent", "item"),
    "get_sellers": ("order-agent", "seller"),
    "get_order_payments": ("payment-agent", "payment"),
    "get_payment_timeline": ("payment-agent", "payment"),
    "get_refund_timeline": ("payment-agent", "refund"),
    "get_shipment_summary": ("shipment-agent", "shipment"),
    "get_policy": ("policy-agent", "policy"),
}
CENT = Decimal("0.01")
ZERO = Decimal("0.00")


class State(Enum):
    RECEIVED = auto()
    DISCOVER = auto()
    COLLECT = auto()
    POLICY = auto()
    VERIFY = auto()
    FINALIZED = auto()


class EvidenceError(ValueError):
    """A result cannot safely be used as authoritative case evidence."""


def money(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise EvidenceError("Missing or invalid monetary value")
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result != result.quantize(CENT):
            raise EvidenceError("Money must be finite, nonnegative and precise to cents")
        return result.quantize(CENT)
    except InvalidOperation as exc:
        raise EvidenceError("Invalid monetary value") from exc


def timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError("Invalid evidence timestamp") from exc
    if result.tzinfo is None:
        raise EvidenceError("Evidence timestamp needs an explicit timezone")
    return result


def rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise EvidenceError("Expected an array of evidence rows")
    return value


def identifiers(records: list[dict[str, Any]], key: str) -> list[str]:
    return sorted({str(row[key]) for row in records if row.get(key) is not None})


@dataclass
class Findings:
    issue: str = "insufficient_evidence"
    tools: set[str] = field(default_factory=lambda: {"get_order"})
    items: list[dict[str, Any]] = field(default_factory=list)
    payments: list[dict[str, Any]] = field(default_factory=list)
    paid: Decimal | None = None
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    uncertain: bool = False
    confidence: float = 0.95
    seller_ids: list[str] = field(default_factory=list)

    def conflict(self, name: str, sources: list[str], selected: str | None, code: str) -> None:
        entry = {
            "field": name,
            "sources": sources,
            "selected_source": selected,
            "resolution_code": code,
        }
        if entry not in self.conflicts:
            self.conflicts.append(entry)
        if selected is None:
            self.uncertain = True


@dataclass
class CaseWorkflow:
    case: dict[str, Any]
    gateway: EvidenceGateway
    trace: TraceWriter
    state: State = State.RECEIVED
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    inventory: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    output: dict[str, Any] | None = None
    findings: Findings | None = None
    timeout_seconds: float = 45.0

    @property
    def case_id(self) -> str:
        return self.case["case_id"]

    @property
    def order_id(self) -> str:
        return self.case["customer_request"]["claimed_order_id"]

    def emit(self, event_type: str, actor: str, **kwargs: Any) -> None:
        self.trace.emit(case_id=self.case_id, event_type=event_type, actor=actor, **kwargs)
        self.events.append(event_type)

    def data(self, tool: str) -> Any:
        return self.evidence[tool]["data"]

    def refs(self, tools: set[str]) -> list[str]:
        return list(dict.fromkeys(self.evidence[name]["evidence_ref"] for name in sorted(tools)))

    async def consume(self, tool: str, actor: str) -> None:
        if TOOLS[tool][0] != actor:
            raise EvidenceError(f"{actor} cannot use {tool}")
        if tool not in self.inventory:
            raise EvidenceError(f"Required MCP tool is unavailable: {tool}")
        arguments = {"case_id": self.case_id}
        if tool == "get_policy":
            arguments["policy_version"] = self.case["policy_version"]
        else:
            arguments["order_id"] = self.order_id
        Draft202012Validator(self.inventory[tool]["inputSchema"]).validate(arguments)
        # No automatic retry: a timed-out request may already have been audited.
        async with asyncio.timeout(self.timeout_seconds):
            result = await self.gateway.call(tool, **arguments)
        self.trace.contracts.validate_evidence(result, tool)
        if result["domain"] != TOOLS[tool][1]:
            raise EvidenceError(f"Wrong evidence domain for {tool}")
        self._check_scope(result["data"])
        if any(
            ev["evidence_ref"] == result["evidence_ref"] and ev != result
            for ev in self.evidence.values()
        ):
            raise EvidenceError("One evidence reference describes different results")
        self.evidence[tool] = copy.deepcopy(result)
        self.emit(
            "tool_result_consumed",
            actor,
            tool_name=tool,
            evidence_refs=[result["evidence_ref"]],
            attributes={"domain": result["domain"], "result_hash": result["result_hash"]},
        )

    def _check_scope(self, value: Any) -> None:
        if isinstance(value, dict):
            if "case_id" in value and value["case_id"] != self.case_id:
                raise EvidenceError("Cross-case evidence rejected")
            if "order_id" in value and value["order_id"] != self.order_id:
                raise EvidenceError("Cross-order evidence rejected")
            for child in value.values():
                self._check_scope(child)
        elif isinstance(value, list):
            for child in value:
                self._check_scope(child)

    async def specialist(self, actor: str, names: list[str]) -> None:
        self.emit("task_assigned", "coordinator", target=actor)
        try:
            for name in names:
                await self.consume(name, actor)
        except (TimeoutError, RuntimeError, ValueError) as exc:
            self.emit(
                "handoff",
                actor,
                target="coordinator",
                decision_code="EVIDENCE_COLLECTION_FAILED",
                attributes={"error_type": type(exc).__name__},
            )
            raise
        self.emit("handoff", actor, target="policy-agent", evidence_refs=self.refs(set(names)))

    async def collect(self) -> None:
        topics = {claim.get("topic") for claim in self.case["customer_request"].get("claims", [])}
        payment_tools = ["get_order_payments", "get_payment_timeline"]
        # Claims route requests, never decide outcomes. The refund domain is not
        # accessible for unrelated cases; do not probe it for every order.
        if topics & {"refund_pending", "refund_failed"}:
            payment_tools.append("get_refund_timeline")
        jobs = [
            ("order-agent", ["get_order", "get_order_items"]),
            ("payment-agent", payment_tools),
            ("shipment-agent", ["get_shipment_summary"]),
        ]
        try:
            async with asyncio.TaskGroup() as group:
                for actor, names in jobs:
                    group.create_task(self.specialist(actor, names))
        except ExceptionGroup as exc:
            raise EvidenceError("Specialist collection failed; case was not finalized") from exc

    async def run(self) -> dict[str, Any]:
        while self.state is not State.FINALIZED:
            if self.state is State.RECEIVED:
                if not self.case_id or not self.order_id or not self.case.get("policy_version"):
                    raise EvidenceError("Case identity, order and policy version are required")
                self.emit("case_received", "coordinator")
                self.state = State.DISCOVER
            elif self.state is State.DISCOVER:
                async with asyncio.timeout(self.timeout_seconds):
                    self.inventory = await self.gateway.describe_tools()
                self.state = State.COLLECT
            elif self.state is State.COLLECT:
                await self.collect()
                self.state = State.POLICY
            elif self.state is State.POLICY:
                self.emit("task_assigned", "coordinator", target="policy-agent")
                await self.consume("get_policy", "policy-agent")
                self.findings = self.analyze()
                if self.findings.issue in {"unavailable_order_paid", "late_delivery_seller"}:
                    await self.specialist("order-agent", ["get_sellers"])
                    self.findings.tools.add("get_sellers")
                    scoped_sellers = identifiers(rows(self.data("get_sellers")), "seller_id")
                    if not set(self.findings.seller_ids) <= set(scoped_sellers):
                        self.findings.conflict(
                            "seller_id",
                            ["get_order_items", "get_sellers"],
                            None,
                            "SELLER_RECORDS_DISAGREE",
                        )
                self.output = self.decide(self.findings)
                self.emit(
                    "policy_decided",
                    "policy-agent",
                    decision_code=self.output["assessment"]["primary_issue"],
                    evidence_refs=self.output["evidence_refs"],
                )
                self.emit(
                    "handoff",
                    "policy-agent",
                    target="verifier",
                    evidence_refs=self.output["evidence_refs"],
                )
                self.state = State.VERIFY
            elif self.state is State.VERIFY:
                self.emit("task_assigned", "coordinator", target="verifier")
                self.verify()
                self.emit(
                    "verification_completed",
                    "verifier",
                    decision_code="VERIFIED",
                    evidence_refs=self.output["evidence_refs"],
                )
                self.emit("case_finalized", "coordinator")
                self.state = State.FINALIZED
        assert self.output is not None
        return self.output

    def analyze(self) -> Findings:
        order, shipment, timeline = (
            self.data(tool)
            for tool in ("get_order", "get_shipment_summary", "get_payment_timeline")
        )
        if not all(isinstance(obj, dict) for obj in (order, shipment, timeline)):
            raise EvidenceError("Invalid order/shipment/payment data shape")
        if order.get("order_id") != self.order_id:
            raise EvidenceError("Authoritative order identity is missing")
        opened = timestamp(self.case.get("opened_at"))
        purchased = timestamp(order.get("order_purchase_timestamp"))
        result = Findings()
        if not opened or not purchased or purchased > opened:
            result.uncertain = True
            return result
        # opened_at is the complaint timestamp, not a snapshot cutoff. A delivery
        # confirmed after opening still resolves that complaint. Use the observed
        # order lifecycle to distinguish unrelated repeated source-row versions.
        delivered_at = timestamp(order.get("order_delivered_customer_date"))
        observed_until = max(opened, delivered_at) if delivered_at else opened

        def active_events(tool: str) -> list[dict[str, Any]]:
            all_events = rows(self.data(tool).get("events", []))
            selected = []
            for event in all_events:
                when = timestamp(event.get("event_at"))
                if when is None:
                    result.uncertain = True
                elif purchased <= when <= observed_until:
                    selected.append(event)
            if len(selected) != len(all_events):
                result.conflict(
                    f"{tool}.events", ["get_order", tool], "get_order", "FILTER_TO_CASE_TIME_WINDOW"
                )
                result.tools.add(tool)
            return sorted(selected, key=lambda event: timestamp(event["event_at"]))

        # Repeated item IDs are versions, not additional purchased items.
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in rows(self.data("get_order_items")):
            if item.get("order_item_id") is None:
                raise EvidenceError("Item identity is missing")
            if item not in grouped[str(item["order_item_id"])]:
                grouped[str(item["order_item_id"])].append(item)
        for candidates in grouped.values():
            valid = candidates
            if len(candidates) > 1:
                valid = [
                    item
                    for item in candidates
                    if (limit := timestamp(item.get("shipping_limit_date"))) is not None
                    and purchased <= limit <= observed_until
                ]
                result.conflict(
                    "items.shipping_limit_date",
                    ["get_order", "get_order_items"],
                    "get_order" if len(valid) == 1 else None,
                    "SELECT_ITEM_VERSION_BY_ORDER_WINDOW"
                    if len(valid) == 1
                    else "AMBIGUOUS_ITEM_VERSIONS",
                )
            if len(valid) == 1:
                result.items.append(valid[0])
        result.tools.add("get_order_items")
        payment_rows = rows(self.data("get_order_payments"))
        result.tools.update({"get_order_payments", "get_payment_timeline"})
        if payment_rows != rows(timeline.get("payments", [])):
            result.conflict(
                "payments",
                ["get_order_payments", "get_payment_timeline"],
                None,
                "PAYMENT_SOURCES_DISAGREE",
            )
        payment_events = active_events("get_payment_timeline")
        captures = [
            event
            for event in payment_events
            if event.get("event_type") == "captured"
            and event.get("status") in {"confirmed", "completed", "succeeded"}
        ]
        amounts = [money(event.get("amount_brl")) for event in captures]
        if captures:
            result.paid = sum(amounts, ZERO)
            remaining = Counter(amounts)
            unique_rows = []
            for row in payment_rows:
                if row not in unique_rows:
                    unique_rows.append(row)
            for row in unique_rows:
                amount = money(row.get("payment_value"))
                if remaining[amount]:
                    result.payments.append(row)
                    remaining[amount] -= 1
            if any(remaining.values()):
                result.conflict(
                    "payments.capture_amounts",
                    ["get_order_payments", "get_payment_timeline"],
                    "get_payment_timeline",
                    "CAPTURE_LEDGER_HAS_UNMATCHED_ROWS",
                )
            if sum((money(row["payment_value"]) for row in payment_rows), ZERO) != result.paid:
                result.conflict(
                    "payments.total",
                    ["get_order_payments", "get_payment_timeline"],
                    "get_payment_timeline",
                    "USE_CONFIRMED_CAPTURES_IN_CASE_WINDOW",
                )

        for left, right in (
            ("order_status", "order_status"),
            ("order_delivered_carrier_date", "delivered_carrier_at"),
            ("order_delivered_customer_date", "delivered_customer_at"),
            ("order_estimated_delivery_date", "estimated_delivery_at"),
        ):
            a, b = order.get(left), shipment.get(right)
            if left != "order_status":
                a, b = timestamp(a), timestamp(b)
            if a != b:
                result.conflict(
                    left,
                    ["get_order", "get_shipment_summary"],
                    None,
                    "AUTHORITATIVE_SOURCES_DISAGREE",
                )
                result.tools.add("get_shipment_summary")

        refund_events = []
        if "get_refund_timeline" in self.evidence:
            result.tools.add("get_refund_timeline")
            refund_events = active_events("get_refund_timeline")
        if refund_events:
            latest = refund_events[-1]
            status = latest.get("status")
            if status == "failed" or latest.get("event_type") == "refund_failed":
                result.issue = "refund_failed"
            elif status in {"pending", "processing", "requested"}:
                result.issue = "refund_pending"
            elif status in {"completed", "confirmed", "succeeded"}:
                result.issue = "unsupported_claim"
            else:
                result.uncertain = True
            return result
        status = order.get("order_status")
        if status in {"canceled", "unavailable"} and result.paid and result.paid > 0:
            result.issue = f"{status}_order_paid"
            result.seller_ids = identifiers(result.items, "seller_id")
            return result
        delivered = timestamp(shipment.get("delivered_customer_at"))
        estimated = timestamp(shipment.get("estimated_delivery_at"))
        carrier = timestamp(shipment.get("delivered_carrier_at"))
        if delivered and estimated and delivered > estimated:
            result.tools.add("get_shipment_summary")
            shipment_events = active_events("get_shipment_summary")
            late_sellers = set()
            limits_complete = bool(result.items) and carrier is not None
            for item in result.items:
                limit = timestamp(item.get("shipping_limit_date"))
                if limit is None:
                    limits_complete = False
                elif carrier and carrier > limit and item.get("seller_id"):
                    late_sellers.add(str(item["seller_id"]))
            actors = {
                event.get("actor")
                for event in shipment_events
                if event.get("event_type") == "delivered_late"
                and event.get("status") == "confirmed"
            }
            if limits_complete:
                role = "seller" if late_sellers else "logistics_provider"
                allowed = {role, "logistics"} if role == "logistics_provider" else {role}
                if actors and not actors <= allowed:
                    result.conflict(
                        "delivery.responsibility",
                        ["get_order_items", "get_shipment_summary"],
                        None,
                        "HANDOFF_AND_ACTOR_DISAGREE",
                    )
                result.issue = "late_delivery_seller" if late_sellers else "late_delivery_logistics"
                result.seller_ids = sorted(late_sellers)
            else:
                result.uncertain = True
            return result
        total = (
            sum(
                (
                    money(item.get("price")) + money(item.get("freight_value"))
                    for item in result.items
                ),
                ZERO,
            )
            if result.items
            else None
        )
        if any(
            event.get("event_type") == "reconciliation_mismatch"
            and event.get("status") in {"open", "confirmed"}
            for event in payment_events
        ):
            result.issue = "payment_mismatch"
        elif result.paid is not None and total is not None:
            if result.paid > total and len(amounts) > 1 and len(set(amounts)) == 1:
                result.issue = "duplicate_charge"
                result.confidence = 0.88
            elif abs(result.paid - total) > CENT:
                result.issue = "payment_mismatch"
            elif len(captures) > 1 and len(result.payments) > 1:
                result.issue = "valid_split_payment"
            elif status == "delivered" and delivered and estimated and delivered <= estimated:
                result.issue = "unsupported_claim"
                result.tools.add("get_shipment_summary")
        return result

    def decide(self, findings: Findings) -> dict[str, Any]:
        policy = self.data("get_policy")
        if (
            not isinstance(policy, dict)
            or policy.get("policy_version") != self.case["policy_version"]
        ):
            raise EvidenceError("Policy version does not match the case")
        if policy.get("currency") != "BRL" or not isinstance(policy.get("rules"), dict):
            raise EvidenceError("Invalid policy currency or rules")
        issue = "insufficient_evidence" if findings.uncertain else findings.issue
        candidate = policy["rules"].get(issue)
        if (
            isinstance(candidate, dict)
            and money(candidate.get("refund_brl")) > ZERO
            and (findings.paid is None or money(candidate["refund_brl"]) > findings.paid)
        ):
            findings.conflict(
                "recommended_refund_brl",
                ["get_policy", "get_payment_timeline"],
                None,
                "REFUND_EXCEEDS_VERIFIED_CAPTURE",
            )
            issue = "insufficient_evidence"
        amount = ZERO
        parties: list[dict[str, Any]] = []
        actions = ["investigate_evidence"]
        status = "needs_investigation"
        tools = findings.tools | {"get_policy"}
        if issue != "insufficient_evidence":
            rule = policy["rules"].get(issue)
            if not isinstance(rule, dict):
                issue = "insufficient_evidence"
            else:
                amount = money(rule.get("refund_brl"))
                status = rule["case_status"]
                actions = [rule["recommended_action"]]
                for party in rows(rule["responsible_parties"]):
                    if party["party_type"] == "seller":
                        # Bind a version-wide policy role to the case's actual seller.
                        sellers = findings.seller_ids or identifiers(findings.items, "seller_id")
                        if not sellers:
                            raise EvidenceError("Seller responsibility has no scoped seller")
                        parties.extend(
                            {"party_type": "seller", "party_id": seller} for seller in sellers
                        )
                    else:
                        parties.append(copy.deepcopy(party))
        warnings = sum(bool(self.evidence[name].get("warnings")) for name in tools)
        confidence = max(
            0.35, min(findings.confidence, 0.95) - 0.05 * len(findings.conflicts) - 0.05 * warnings
        )
        if issue == "insufficient_evidence":
            confidence = 0.35
        refs = self.refs(tools)
        entities = {
            "order_ids": [self.order_id],
            "item_ids": identifiers(findings.items, "order_item_id"),
            "seller_ids": identifiers(findings.items, "seller_id"),
            "payment_references": identifiers(findings.payments, "payment_reference"),
            "shipment_ids": [],
        }
        shipment = self.data("get_shipment_summary")
        if "get_shipment_summary" in tools and shipment.get("shipment_id"):
            entities["shipment_ids"] = [str(shipment["shipment_id"])]
        claims = []
        for claim in self.case["customer_request"].get("claims", []):
            topic = claim.get("topic")
            verdict = "insufficient_evidence"
            if issue != "insufficient_evidence":
                if topic == issue:
                    verdict = "supported"
                elif topic == "requested_full_refund":
                    if amount == 0:
                        verdict = (
                            "unsupported" if status == "no_action" else "insufficient_evidence"
                        )
                    elif findings.paid is not None:
                        verdict = "supported" if amount == findings.paid else "partially_supported"
                elif topic in policy["rules"]:
                    verdict = "unsupported"
            claims.append(
                {
                    "claim_id": claim["claim_id"],
                    "verdict": verdict,
                    "confidence": round(confidence, 2),
                    "evidence_refs": refs,
                }
            )
        return {
            "schema_version": "day09-l3a-output-v2",
            "case_id": self.case_id,
            "assessment": {
                "primary_issue": issue,
                "case_status": status,
                "confidence": round(confidence, 2),
            },
            "affected_entities": entities,
            "claim_assessments": claims,
            "root_cause_analysis": {
                "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
                "responsible_parties": parties,
            },
            "evidence_refs": refs,
            "data_conflicts": findings.conflicts[:5],
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": float(amount),
                "refund_lines": [
                    {"reason_code": issue, "amount_brl": float(amount), "entity_id": self.order_id}
                ]
                if amount
                else [],
            },
            "resolution_actions": actions,
        }

    def verify(self) -> None:
        if self.output is None or self.findings is None:
            raise EvidenceError("No policy decision to verify")
        output = self.output
        self.trace.contracts.validate_output(output, f"case {self.case_id}")
        if output["case_id"] != self.case_id:
            raise EvidenceError("Output case identity changed")
        issued = {record["evidence_ref"] for record in self.evidence.values()}
        cited = set(output["evidence_refs"])
        if not cited or not cited <= issued:
            raise EvidenceError("Output contains unconsumed evidence references")
        for claim in output.get("claim_assessments", []):
            if not set(claim["evidence_refs"]) <= cited:
                raise EvidenceError("Claim references are not linked to the decision")
        financial = output["financial_resolution"]
        amount = money(financial["recommended_refund_brl"])
        if amount != sum((money(line["amount_brl"]) for line in financial["refund_lines"]), ZERO):
            raise EvidenceError("Refund lines do not sum to the recommendation")
        if any(line["entity_id"] != self.order_id for line in financial["refund_lines"]):
            raise EvidenceError("Refund line has an out-of-scope entity")
        assessment = output["assessment"]
        issue = assessment["primary_issue"]
        parties = output["root_cause_analysis"]["responsible_parties"]
        if issue != "insufficient_evidence":
            rule = self.data("get_policy")["rules"][issue]
            if (
                amount != money(rule["refund_brl"])
                or assessment["case_status"] != rule["case_status"]
                or output["resolution_actions"] != [rule["recommended_action"]]
                or {party["party_type"] for party in parties}
                != {party["party_type"] for party in rule["responsible_parties"]}
            ):
                raise EvidenceError("Decision is inconsistent with the authoritative policy")
        elif amount or parties or assessment["case_status"] != "needs_investigation":
            raise EvidenceError("Incomplete evidence cannot authorize a refund or assign blame")
        if assessment["case_status"] == "no_action" and amount:
            raise EvidenceError("A no-action case cannot recommend a refund")
        if amount and (self.findings.paid is None or amount > self.findings.paid):
            raise EvidenceError("Refund exceeds verified capture")
        for party in parties:
            if (
                party["party_type"] == "seller"
                and party["party_id"] not in output["affected_entities"]["seller_ids"]
            ):
                raise EvidenceError("Responsible seller is outside the affected case")
        types = {party["party_type"] for party in parties}
        if issue == "late_delivery_seller" and types != {"seller"}:
            raise EvidenceError("Seller delay assigned to a different party")
        if issue == "late_delivery_logistics" and types != {"logistics_provider"}:
            raise EvidenceError("Logistics delay assigned to a different party")
        if output["data_conflicts"] and assessment["confidence"] > 0.9:
            raise EvidenceError("Confidence is too high for conflicting evidence")
        for conflict in output["data_conflicts"]:
            if conflict["selected_source"] not in [None, *conflict["sources"]]:
                raise EvidenceError("Conflict selected source is not one of its sources")
        scoring_path = self.trace.contracts.root.parent / "scoring/scoring-policy-v2.json"
        scoring = json.loads(scoring_path.read_text(encoding="utf-8"))
        expected = set(scoring["workflow_required_events"]) | {
            "tool_result_consumed",
            "policy_decided",
        }
        if not (expected - {"case_finalized", "verification_completed"}) <= set(self.events):
            raise EvidenceError("Workflow lifecycle is incomplete")


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run a case-local async state machine with bounded specialists and verification.

    Owns the complete trace lifecycle. Failures propagate without case_finalized
    or a fabricated answer. No evidence is cached or reused between cases.
    """
    return await CaseWorkflow(copy.deepcopy(case), gateway, trace).run()
