import asyncio
import json
from types import SimpleNamespace

import pytest

from student_agent import cli
from student_agent.contracts import Contracts
from student_agent.resume import completed_cases
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case
from test_workflow import ROOT, FakeGateway, fixture


def saved_case(tmp_path):
    case, data = fixture()
    contracts = Contracts(ROOT / "contracts/schemas")
    trace = TraceWriter(tmp_path / "traces/trace.jsonl", contracts)
    output = asyncio.run(solve_case(case, FakeGateway(case, data), trace))
    (tmp_path / "outputs").mkdir()
    path = tmp_path / "outputs" / f"{case['case_id']}.json"
    path.write_text(json.dumps(output), encoding="utf-8")
    cases = SimpleNamespace(case_ids=(case["case_id"],), cases={case["case_id"]: case})
    return cases, contracts, path, trace.path


def test_resume_preserves_files_and_accepts_verified_output(tmp_path):
    cases, contracts, path, trace_path = saved_case(tmp_path)
    before = (path.read_bytes(), trace_path.read_bytes())
    assert completed_cases(tmp_path, cases, contracts) == set(cases.case_ids)
    assert before == (path.read_bytes(), trace_path.read_bytes())


@pytest.mark.parametrize("damage", ["missing_trace", "no_finalize", "ref", "order", "json"])
def test_resume_rejects_unverifiable_artifacts(tmp_path, damage):
    cases, contracts, path, trace_path = saved_case(tmp_path)
    if damage == "missing_trace":
        trace_path.unlink()
    elif damage == "no_finalize":
        events = trace_path.read_text().splitlines()
        trace_path.write_text("\n".join(events[:-1]) + "\n")
    elif damage == "json":
        trace_path.write_text(trace_path.read_text() + '{"partial":')
    else:
        output = json.loads(path.read_text())
        if damage == "ref":
            output["evidence_refs"] = ["ev_TEST_UNKNOWN_REFERENCE_000000"]
        else:
            output["affected_entities"]["order_ids"] = ["other"]
        path.write_text(json.dumps(output))
    with pytest.raises(ValueError):
        completed_cases(tmp_path, cases, contracts)


def test_resume_keeps_partial_trace_but_does_not_skip_missing_output(tmp_path):
    cases, contracts, path, trace_path = saved_case(tmp_path)
    path.unlink()
    assert completed_cases(tmp_path, cases, contracts) == set()
    assert trace_path.exists()


def test_cli_resume_only_calls_missing_case_and_preserves_completed(tmp_path, monkeypatch):
    cases, contracts, path, trace_path = saved_case(tmp_path)
    original = path.read_bytes(), trace_path.read_bytes()
    second, _ = fixture()
    second["case_id"] = "TEST_CASE_002"
    cases.case_ids += (second["case_id"],)
    cases.cases[second["case_id"]] = second
    monkeypatch.setattr(cli.Settings, "load", lambda root: None)
    monkeypatch.setattr(cli, "load_case_set", lambda root: cases)
    monkeypatch.setattr(cli, "Contracts", lambda root: contracts)
    calls = []

    async def fail(case, *args):
        calls.append(case["case_id"])
        raise RuntimeError("network still down")

    monkeypatch.setattr(cli, "solve_with_reconnect", fail)
    with pytest.raises(RuntimeError):
        asyncio.run(cli._run(tmp_path, resume=True))
    assert calls == ["TEST_CASE_002"]
    assert original == (path.read_bytes(), trace_path.read_bytes())


def test_resume_flag():
    assert cli.parser().parse_args(["run", "--resume"]).resume
