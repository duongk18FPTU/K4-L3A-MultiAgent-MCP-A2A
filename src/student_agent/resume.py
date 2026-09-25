"""Validate local completed artifacts before continuing the same competition run."""

from __future__ import annotations

import json
from pathlib import Path

from .cases import CaseSet
from .contracts import Contracts


def completed_cases(root: Path, case_set: CaseSet, contracts: Contracts) -> set[str]:
    paths = {path.stem: path for path in (root / "outputs").glob("*.json")}
    expected = set(case_set.case_ids)
    if set(paths) - expected:
        raise ValueError("Resume found output files outside the current case-set")
    trace_path = root / "traces/trace.jsonl"
    if not trace_path.exists():
        if paths:
            raise ValueError("Cannot resume outputs without their original trace")
        return set()
    attempts: dict[str, dict] = {}
    finalized: dict[str, list[dict]] = {}
    seen_ids = set()
    for number, line in enumerate(trace_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        event = json.loads(line)
        contracts.validate_trace(event, f"resume trace:{number}")
        case_id = event["case_id"]
        if case_id not in expected or event["event_id"] in seen_ids:
            raise ValueError("Resume trace contains an invalid case or duplicate event ID")
        seen_ids.add(event["event_id"])
        kind = event["event_type"]
        if kind == "case_received":
            attempts[case_id] = {"consumed": set(), "verified": None, "policy": None}
        attempt = attempts.get(case_id)
        if attempt is None:
            continue
        refs = set(event.get("evidence_refs", []))
        if kind == "tool_result_consumed":
            attempt["consumed"].update(refs)
        elif kind == "policy_decided":
            attempt["policy"] = (event.get("decision_code"), refs)
        elif kind == "verification_completed":
            attempt["verified"] = refs
        elif kind == "case_finalized":
            finalized.setdefault(case_id, []).append(attempt)
            del attempts[case_id]
    for case_id, path in paths.items():
        output = json.loads(path.read_text(encoding="utf-8"))
        contracts.validate_output(output, str(path))
        if output["case_id"] != case_id:
            raise ValueError(f"Resume case_id mismatch: {case_id}")
        order_id = case_set.cases[case_id]["customer_request"]["claimed_order_id"]
        if output["affected_entities"]["order_ids"] != [order_id]:
            raise ValueError(f"Resume order scope mismatch: {case_id}")
        refs = set(output["evidence_refs"])
        issue = output["assessment"]["primary_issue"]
        if not refs or not any(
            refs <= attempt["consumed"]
            and refs == attempt["verified"]
            and attempt["policy"] == (issue, refs)
            for attempt in finalized.get(case_id, [])
        ):
            raise ValueError(f"Resume output lacks a matching verified/finalized trace: {case_id}")
        if any(
            not set(claim["evidence_refs"]) <= refs for claim in output.get("claim_assessments", [])
        ):
            raise ValueError(f"Resume claim evidence mismatch: {case_id}")
    return set(paths)
