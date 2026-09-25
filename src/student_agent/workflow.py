from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
}

CAUSE_CODES = {
    "canceled_order_paid": "ORDER_CANCELED_AFTER_PAYMENT",
    "unavailable_order_paid": "ORDER_UNAVAILABLE_AFTER_PAYMENT",
    "late_delivery_seller": "SELLER_HANDOFF_DELAY",
    "late_delivery_logistics": "LOGISTICS_DELIVERY_DELAY",
    "valid_split_payment": "VALID_SPLIT_PAYMENT",
    "payment_mismatch": "PAYMENT_RECONCILIATION_MISMATCH",
    "duplicate_charge": "DUPLICATE_PAYMENT_CAPTURE",
    "refund_pending": "REFUND_PENDING",
    "refund_failed": "REFUND_FAILED",
    "unsupported_claim": "CLAIM_NOT_SUPPORTED",
    "insufficient_evidence": "INSUFFICIENT_EVIDENCE",
}

TOOL_ACTORS = {
    "get_order": "order-item-agent",
    "get_order_items": "order-item-agent",
    "get_payment_timeline": "payment-agent",
    "get_refund_timeline": "payment-agent",
    "get_shipment_summary": "shipment-agent",
    "get_policy": "policy-agent",
}

TOOL_PERMISSIONS = {
    "order-item-agent": {"get_order", "get_order_items"},
    "payment-agent": {"get_payment_timeline", "get_refund_timeline"},
    "shipment-agent": {"get_shipment_summary"},
    "policy-agent": {"get_policy"},
}


@dataclass(frozen=True)
class Evidence:
    tool_name: str
    actor: str
    evidence_ref: str
    data: Any


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _topic(case: dict[str, Any]) -> str | None:
    claims = case.get("customer_request", {}).get("claims", [])
    for claim in claims:
        topic = claim.get("topic")
        if topic != "requested_full_refund":
            return topic if isinstance(topic, str) else None
    return None


def _tool_plan(topic: str | None) -> tuple[str, ...]:
    tools = ["get_order", "get_order_items"]
    if topic in {
        "canceled_order_paid",
        "unavailable_order_paid",
        "valid_split_payment",
        "payment_mismatch",
        "duplicate_charge",
        "unsupported_claim",
    }:
        tools.append("get_payment_timeline")
    if topic in {"late_delivery_seller", "late_delivery_logistics", "unsupported_claim"}:
        tools.append("get_shipment_summary")
    if topic in {"refund_pending", "refund_failed"}:
        tools.append("get_refund_timeline")
    tools.append("get_policy")
    return tuple(tools)


def _arguments(tool_name: str, *, order_id: str, policy_version: str) -> dict[str, str]:
    if tool_name == "get_policy":
        return {"policy_version": policy_version}
    return {"order_id": order_id}


def _in_scope(tool_name: str, data: Any, order_id: str, policy_version: str) -> bool:
    if tool_name == "get_policy":
        return isinstance(data, dict) and data.get("policy_version") == policy_version
    if tool_name == "get_order_items":
        return isinstance(data, list) and all(
            isinstance(row, dict) and row.get("order_id") == order_id for row in data
        )
    return isinstance(data, dict) and data.get("order_id") == order_id


async def _collect_evidence(
    *,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    case_id: str,
    order_id: str,
    policy_version: str,
    tool_name: str,
    discovered: set[str],
) -> tuple[Evidence | None, str | None]:
    actor = TOOL_ACTORS[tool_name]
    if tool_name not in TOOL_PERMISSIONS[actor]:
        return None, "permission_denied"
    if tool_name not in discovered:
        return None, "tool_not_discovered"

    arguments = _arguments(tool_name, order_id=order_id, policy_version=policy_version)
    for attempt in range(2):
        try:
            envelope = await gateway.call(tool_name, case_id=case_id, **arguments)
            data = envelope["data"]
            if not _in_scope(tool_name, data, order_id, policy_version):
                return None, "out_of_scope_response"
            evidence = Evidence(
                tool_name=tool_name,
                actor=actor,
                evidence_ref=envelope["evidence_ref"],
                data=data,
            )
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[evidence.evidence_ref],
                attributes={"attempt": attempt + 1},
            )
            return evidence, None
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            if attempt == 0:
                await asyncio.sleep(0.25)
                continue
            return None, type(exc).__name__
    return None, "unreachable"


def _select_items(rows: Any, anchor: datetime | None) -> tuple[list[dict[str, Any]], list[int]]:
    if not isinstance(rows, list):
        return [], []
    groups: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, row in enumerate(rows):
        if isinstance(row, dict):
            key = str(row.get("order_item_id", f"row-{index}"))
            groups.setdefault(key, []).append((index, row))

    selected: list[dict[str, Any]] = []
    indexes: list[int] = []
    for candidates in groups.values():
        if anchor is None:
            index, row = candidates[0]
        else:
            index, row = min(
                candidates,
                key=lambda candidate: abs(
                    (
                        (_parse_time(candidate[1].get("shipping_limit_date")) or anchor) - anchor
                    ).total_seconds()
                ),
            )
        selected.append(row)
        indexes.append(index)
    return selected, indexes


def _near_events(
    events: Any, anchor: datetime | None, *, window: timedelta
) -> tuple[list[dict[str, Any]], list[int]]:
    if not isinstance(events, list):
        return [], []
    valid = [(index, row) for index, row in enumerate(events) if isinstance(row, dict)]
    if anchor is None:
        return [row for _, row in valid], [index for index, _ in valid]
    selected = [
        (index, row)
        for index, row in valid
        if (stamp := _parse_time(row.get("event_at"))) is not None
        and abs((stamp - anchor).total_seconds()) <= window.total_seconds()
    ]
    return [row for _, row in selected], [index for index, _ in selected]


def _analysis_context(evidence: dict[str, Evidence]) -> dict[str, Any]:
    order = evidence.get("get_order")
    order_data = order.data if order and isinstance(order.data, dict) else {}
    approved = _parse_time(order_data.get("order_approved_at"))
    delivered = _parse_time(order_data.get("order_delivered_customer_date"))

    items_envelope = evidence.get("get_order_items")
    items, selected_item_indexes = _select_items(
        items_envelope.data if items_envelope else [], approved
    )

    payment_envelope = evidence.get("get_payment_timeline")
    payment_data = (
        payment_envelope.data
        if payment_envelope and isinstance(payment_envelope.data, dict)
        else {}
    )
    payment_events, selected_payment_indexes = _near_events(
        payment_data.get("events", []), approved, window=timedelta(days=2)
    )

    shipment_envelope = evidence.get("get_shipment_summary")
    shipment_data = (
        shipment_envelope.data
        if shipment_envelope and isinstance(shipment_envelope.data, dict)
        else {}
    )
    shipment_anchor = _parse_time(shipment_data.get("delivered_customer_at")) or delivered
    shipment_events, selected_shipment_indexes = _near_events(
        shipment_data.get("events", []), shipment_anchor, window=timedelta(days=1)
    )

    refund_envelope = evidence.get("get_refund_timeline")
    refund_data = (
        refund_envelope.data if refund_envelope and isinstance(refund_envelope.data, dict) else {}
    )
    refund_events = [row for row in refund_data.get("events", []) if isinstance(row, dict)]

    expected_total = sum(
        (_money(row.get("price")) + _money(row.get("freight_value")) for row in items),
        Decimal("0"),
    )
    captures = [
        row
        for row in payment_events
        if row.get("event_type") == "captured" and row.get("status") == "confirmed"
    ]
    captured_total = sum((_money(row.get("amount_brl")) for row in captures), Decimal("0"))

    return {
        "order": order_data,
        "items": items,
        "selected_item_indexes": selected_item_indexes,
        "all_items": items_envelope.data if items_envelope else [],
        "payment_events": payment_events,
        "selected_payment_indexes": selected_payment_indexes,
        "all_payment_events": payment_data.get("events", []),
        "shipment": shipment_data,
        "shipment_events": shipment_events,
        "selected_shipment_indexes": selected_shipment_indexes,
        "all_shipment_events": shipment_data.get("events", []),
        "refund_events": refund_events,
        "expected_total": expected_total,
        "captures": captures,
        "captured_total": captured_total,
    }


def _required_evidence_present(topic: str | None, evidence: dict[str, Evidence]) -> bool:
    return set(_tool_plan(topic)).issubset(evidence)


def _verify_topic(topic: str | None, context: dict[str, Any]) -> bool:
    order = context["order"]
    captures = context["captures"]
    payment_events = context["payment_events"]
    shipment_events = context["shipment_events"]
    refund_events = context["refund_events"]
    expected_total = context["expected_total"]
    captured_total = context["captured_total"]

    if topic == "canceled_order_paid":
        return order.get("order_status") == "canceled" and bool(captures)
    if topic == "unavailable_order_paid":
        return order.get("order_status") == "unavailable" and bool(captures)
    if topic == "late_delivery_seller":
        return any(
            row.get("event_type") == "delivered_late"
            and row.get("actor") == "seller"
            and row.get("status") == "confirmed"
            for row in shipment_events
        )
    if topic == "late_delivery_logistics":
        return any(
            row.get("event_type") == "delivered_late"
            and row.get("actor") == "logistics_provider"
            and row.get("status") == "confirmed"
            for row in shipment_events
        )
    if topic == "valid_split_payment":
        return (
            len(captures) >= 2
            and expected_total > 0
            and abs(captured_total - expected_total) <= Decimal("0.01")
        )
    if topic == "payment_mismatch":
        return any(
            row.get("event_type") == "reconciliation_mismatch"
            and row.get("status") in {"open", "failed"}
            for row in payment_events
        ) or (expected_total > 0 and abs(captured_total - expected_total) > Decimal("0.01"))
    if topic == "duplicate_charge":
        return expected_total > 0 and captured_total > expected_total + Decimal("0.01")
    if topic == "refund_pending":
        return any(row.get("status") == "pending" for row in refund_events)
    if topic == "refund_failed":
        return any(row.get("status") == "failed" for row in refund_events)
    if topic == "unsupported_claim":
        delivered = _parse_time(order.get("order_delivered_customer_date"))
        estimated = _parse_time(order.get("order_estimated_delivery_date"))
        on_time = delivered is not None and estimated is not None and delivered <= estimated
        payment_ok = expected_total > 0 and abs(captured_total - expected_total) <= Decimal("0.01")
        anomalies = any(
            row.get("event_type") in {"reconciliation_mismatch", "delivered_late"}
            for row in [*payment_events, *shipment_events]
        )
        return order.get("order_status") == "delivered" and on_time and payment_ok and not anomalies
    return False


def _evidence_refs_for_topic(topic: str | None, evidence: dict[str, Evidence]) -> list[str]:
    names = ["get_order", "get_order_items"]
    if topic in {
        "canceled_order_paid",
        "unavailable_order_paid",
        "valid_split_payment",
        "payment_mismatch",
        "duplicate_charge",
        "unsupported_claim",
    }:
        names.append("get_payment_timeline")
    if topic in {"late_delivery_seller", "late_delivery_logistics", "unsupported_claim"}:
        names.append("get_shipment_summary")
    if topic in {"refund_pending", "refund_failed"}:
        names.append("get_refund_timeline")
    names.append("get_policy")
    return [evidence[name].evidence_ref for name in names if name in evidence]


def _conflicts(context: dict[str, Any]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    all_items = context["all_items"] if isinstance(context["all_items"], list) else []
    selected_items = set(context["selected_item_indexes"])
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, row in enumerate(all_items):
        if isinstance(row, dict):
            grouped.setdefault(str(row.get("order_item_id", index)), []).append((index, row))
    for item_id, rows in grouped.items():
        if len(rows) < 2:
            continue
        for field in ("shipping_limit_date", "price", "freight_value"):
            if len({str(row.get(field)) for _, row in rows}) < 2:
                continue
            chosen = next((index for index, _ in rows if index in selected_items), rows[0][0])
            selected_source = f"get_order_items[row:{chosen}]"
            sources = [f"get_order_items[row:{index}]" for index, _ in rows][:5]
            if selected_source not in sources:
                sources[-1] = selected_source
            conflicts.append(
                {
                    "field": f"items.{item_id}.{field}"[:100],
                    "sources": sources,
                    "selected_source": selected_source,
                    "resolution_code": "NEAREST_TO_ORDER_APPROVAL",
                }
            )
            if len(conflicts) == 5:
                return conflicts

    for key, tool_name, selected_key in (
        ("all_payment_events", "get_payment_timeline", "selected_payment_indexes"),
        ("all_shipment_events", "get_shipment_summary", "selected_shipment_indexes"),
    ):
        rows = context[key] if isinstance(context[key], list) else []
        selected = context[selected_key]
        if rows and selected and len(selected) < len(rows):
            selected_source = f"{tool_name}[event:{selected[0]}]"
            sources = [f"{tool_name}[event:{index}]" for index in range(len(rows))][:5]
            if selected_source not in sources:
                sources[-1] = selected_source
            conflicts.append(
                {
                    "field": f"{tool_name}.events",
                    "sources": sources,
                    "selected_source": selected_source,
                    "resolution_code": "MATCHED_ORDER_TIMELINE",
                }
            )
        if len(conflicts) == 5:
            break
    return conflicts


def _claim_assessments(
    case: dict[str, Any],
    *,
    topic: str | None,
    primary_issue: str,
    verified: bool,
    complete: bool,
    refund_amount: float,
    case_status: str,
    evidence_refs: list[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    claims = case.get("customer_request", {}).get("claims", [])
    for claim in claims[:5]:
        claim_id = str(claim.get("claim_id", "unknown-claim"))[:64]
        claim_topic = claim.get("topic")
        if claim_topic == topic:
            verdict = (
                "supported"
                if verified
                else ("unsupported" if complete else "insufficient_evidence")
            )
            confidence = 0.96 if verified else (0.82 if complete else 0.35)
        elif claim_topic == "requested_full_refund":
            if not complete or case_status == "needs_investigation":
                verdict, confidence = "insufficient_evidence", 0.55
            elif refund_amount <= 0:
                verdict, confidence = "unsupported", 0.94
            elif primary_issue in {
                "canceled_order_paid",
                "unavailable_order_paid",
                "refund_failed",
            }:
                verdict, confidence = "supported", 0.93
            else:
                verdict, confidence = "partially_supported", 0.9
        else:
            verdict, confidence = "unsupported", 0.8
        result.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": evidence_refs,
            }
        )
    return result


def _build_output(
    case: dict[str, Any], topic: str | None, evidence: dict[str, Evidence]
) -> tuple[dict[str, Any], str]:
    context = _analysis_context(evidence)
    complete = _required_evidence_present(topic, evidence)
    verified = complete and _verify_topic(topic, context)

    if not complete:
        primary_issue = "insufficient_evidence"
    elif verified and topic in ISSUES:
        primary_issue = topic
    else:
        primary_issue = "unsupported_claim"

    policy = evidence.get("get_policy")
    policy_data = policy.data if policy and isinstance(policy.data, dict) else {}
    rules = policy_data.get("rules", {}) if isinstance(policy_data.get("rules", {}), dict) else {}
    rule = rules.get(topic, {}) if verified and isinstance(topic, str) else {}
    if not isinstance(rule, dict):
        rule = {}

    has_rule = bool(rule)
    case_status = str(rule.get("case_status", "needs_investigation"))
    if case_status not in {"action_required", "no_action", "needs_investigation"}:
        case_status = "needs_investigation"
    refund_amount = float(_money(rule.get("refund_brl", 0))) if has_rule else 0.0
    action = str(rule.get("recommended_action", "request_authoritative_evidence"))[:80]
    parties = rule.get("responsible_parties", []) if has_rule else []
    responsible_parties = [
        {"party_type": row.get("party_type", "unknown"), "party_id": row.get("party_id")}
        for row in parties
        if isinstance(row, dict)
        and row.get("party_type")
        in {"seller", "platform", "logistics_provider", "payment_provider", "customer", "unknown"}
    ]
    if not responsible_parties:
        responsible_parties = [{"party_type": "unknown", "party_id": None}]

    selected_items = context["items"]
    order_id = case["customer_request"]["claimed_order_id"]
    refs = _evidence_refs_for_topic(topic, evidence)
    all_refs = _unique([item.evidence_ref for item in evidence.values()])
    confidence = 0.96 if verified and has_rule else (0.82 if complete else 0.35)
    refund_lines = []
    if refund_amount > 0:
        refund_lines.append(
            {
                "reason_code": action,
                "amount_brl": refund_amount,
                "entity_id": responsible_parties[0]["party_id"],
            }
        )

    output = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [order_id],
            "item_ids": _unique(
                [str(row["order_item_id"]) for row in selected_items if row.get("order_item_id")]
            ),
            "seller_ids": _unique(
                [str(row["seller_id"]) for row in selected_items if row.get("seller_id")]
            ),
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": _claim_assessments(
            case,
            topic=topic,
            primary_issue=primary_issue,
            verified=verified,
            complete=complete,
            refund_amount=refund_amount,
            case_status=case_status,
            evidence_refs=refs,
        ),
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": CAUSE_CODES[primary_issue], "rank": 1}],
            "responsible_parties": responsible_parties,
        },
        "evidence_refs": all_refs,
        "data_conflicts": _conflicts(context),
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_amount,
            "refund_lines": refund_lines,
        },
        "resolution_actions": [action],
    }
    decision = "VERIFIED" if verified and has_rule else "NEEDS_INVESTIGATION"
    return output, decision


def _verify_output_invariants(output: dict[str, Any], case_id: str) -> None:
    if output["case_id"] != case_id:
        raise ValueError("verifier rejected mismatched case_id")
    refs = output["evidence_refs"]
    if len(refs) != len(set(refs)):
        raise ValueError("verifier rejected duplicate evidence references")
    financial = output["financial_resolution"]
    line_total = round(sum(line["amount_brl"] for line in financial["refund_lines"]), 2)
    if line_total != round(financial["recommended_refund_brl"], 2):
        raise ValueError("verifier rejected inconsistent refund total")
    confidence = output["assessment"]["confidence"]
    if not 0 <= confidence <= 1:
        raise ValueError("verifier rejected confidence outside [0, 1]")


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run a bounded coordinator/specialist/verifier workflow for one L3A case."""
    case_id = str(case["case_id"])
    request = case.get("customer_request", {})
    order_id = str(request["claimed_order_id"])
    policy_version = str(case["policy_version"])
    topic = _topic(case)
    plan = _tool_plan(topic)
    discovered = set(await gateway.list_tools())

    actors = _unique([TOOL_ACTORS[name] for name in plan])
    for actor in actors:
        assigned = [name for name in plan if TOOL_ACTORS[name] == actor]
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            decision_code="ROUTE_BY_CLAIM_HYPOTHESIS",
            attributes={"tool_count": len(assigned), "topic": topic or "unknown"},
        )

    semaphore = asyncio.Semaphore(2)

    async def collect(tool_name: str) -> tuple[Evidence | None, str | None]:
        async with semaphore:
            return await _collect_evidence(
                gateway=gateway,
                trace=trace,
                case_id=case_id,
                order_id=order_id,
                policy_version=policy_version,
                tool_name=tool_name,
                discovered=discovered,
            )

    collected = await asyncio.gather(*(collect(tool_name) for tool_name in plan))

    evidence: dict[str, Evidence] = {}
    failures: dict[str, str] = {}
    for tool_name, (item, failure) in zip(plan, collected, strict=True):
        if item is not None:
            evidence[tool_name] = item
        elif failure is not None:
            failures[tool_name] = failure

    for actor in actors:
        actor_refs = [item.evidence_ref for item in evidence.values() if item.actor == actor]
        actor_failures = [name for name in failures if TOOL_ACTORS[name] == actor]
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=actor,
            target="coordinator",
            decision_code="EVIDENCE_READY" if not actor_failures else "PARTIAL_EVIDENCE",
            evidence_refs=actor_refs or None,
            attributes={"failure_count": len(actor_failures)},
        )

    policy_evidence = evidence.get("get_policy")
    policy_rules = (
        policy_evidence.data.get("rules", {})
        if policy_evidence and isinstance(policy_evidence.data, dict)
        else {}
    )
    policy_matched = isinstance(policy_rules, dict) and topic in policy_rules
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        target="coordinator",
        decision_code="POLICY_MATCHED" if policy_matched else "POLICY_UNAVAILABLE",
        evidence_refs=[policy_evidence.evidence_ref] if policy_evidence else None,
        attributes={"policy_version": policy_version},
    )

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="verifier-agent",
        decision_code="VERIFY_CONTRACT_AND_SEMANTICS",
    )
    output, decision = _build_output(case, topic, evidence)
    _verify_output_invariants(output, case_id)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        target="coordinator",
        decision_code=decision,
        evidence_refs=output["evidence_refs"][:20] or None,
        attributes={"failure_count": len(failures)},
    )
    return output
