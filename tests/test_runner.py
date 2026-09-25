from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx2
import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

from student_agent import runner
from student_agent.contracts import ContractError, Contracts
from student_agent.mcp_gateway import GatewayHTTPError
from student_agent.trace import TraceWriter
from student_agent.workflow import EvidenceError
from test_workflow import ROOT, FakeGateway, fixture


@pytest.mark.parametrize(
    "exc",
    [
        httpx2.ReadError("read interrupted"),
        httpx2.ConnectError("unreachable"),
        TimeoutError(),
        ExceptionGroup("transport", [httpx2.ReadError("read"), httpx2.WriteError("write")]),
    ],
)
def test_transient_errors(exc):
    assert runner.is_transient_transport(exc)


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("403 Forbidden"),
        ContractError("extra field"),
        EvidenceError("wrong scope"),
        asyncio.CancelledError(),
        TypeError("programming error"),
        ExceptionGroup("mixed", [httpx2.ReadError("read"), ContractError("bad schema")]),
    ],
)
def test_nontransport_errors_are_not_retryable(exc):
    assert not runner.is_transient_transport(exc)


@pytest.mark.parametrize("code,expected", [(403, False), (401, False), (429, True), (503, True)])
def test_http_status_retry_policy(code, expected):
    request = httpx2.Request("POST", "https://example.invalid/mcp")
    response = httpx2.Response(code, request=request)
    error = httpx2.HTTPStatusError("status", request=request, response=response)
    assert runner.is_transient_transport(error) is expected


def test_mcp_closed_channel_and_timeout_are_transport_failures():
    assert runner.is_transient_transport(
        ExceptionGroup(
            "SDK transport failure",
            [
                httpx2.ReadError("read"),
                MCPError(CONNECTION_CLOSED, "Connection closed"),
            ],
        )
    )
    assert runner.is_transient_transport(MCPError(REQUEST_TIMEOUT, "Request timed out"))
    assert not runner.is_transient_transport(MCPError(CONNECTION_CLOSED, "403 Forbidden"))


@pytest.mark.parametrize("status,expected", [(401, False), (403, False), (500, True), (429, True)])
def test_preserved_http_status_is_classified(status, expected):
    assert (
        runner.is_transient_transport(
            ExceptionGroup("stream failed", [GatewayHTTPError(status)]),
        )
        is expected
    )


def setup(tmp_path, monkeypatch, *, failure=None, always=False, during="tool"):
    case, data = fixture("valid_split_payment")
    contracts = Contracts(ROOT / "contracts/schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    sessions, delays = [], []

    class Gateway(FakeGateway):
        async def call(self, name, **arguments):
            if (always or self.attempt == 1) and during == "tool" and name == "get_order_items":
                raise failure
            value = await super().call(name, **arguments)
            value["evidence_ref"] = value["evidence_ref"].replace(
                "UNIT_TEST_ONLY",
                f"TEST_ATTEMPT_{self.attempt}",
            )
            return value

    @asynccontextmanager
    async def connect(*args):
        gateway = Gateway(case, data)
        gateway.attempt = len(sessions) + 1
        sessions.append(gateway)
        if (always or gateway.attempt == 1) and during == "connect":
            raise failure
        yield gateway
        if during == "close":
            raise ExceptionGroup("close failed", [failure])

    async def sleep(delay):
        if delay:
            delays.append(delay)

    monkeypatch.setattr(runner, "connect_gateway", connect)
    monkeypatch.setattr(runner.asyncio, "sleep", sleep)
    settings = SimpleNamespace(mcp_endpoint="unused", team_api_key="unused")
    return case, settings, contracts, trace, sessions, delays


def test_read_error_reconnects_and_uses_only_fresh_evidence(tmp_path, monkeypatch):
    case, settings, contracts, trace, sessions, delays = setup(
        tmp_path,
        monkeypatch,
        failure=httpx2.ReadError("broken stream"),
    )
    output = asyncio.run(runner.solve_with_reconnect(case, settings, contracts, trace))
    assert len(sessions) == 2 and sessions[0] is not sessions[1]
    assert delays == [1]
    assert all("TEST_ATTEMPT_2" in ref for ref in output["evidence_refs"])
    events = [json.loads(line) for line in trace.path.read_text().splitlines()]
    assert events[0]["event_type"] == "case_received"
    assert sum(event["event_type"] == "case_finalized" for event in events) == 1
    assert any(event.get("decision_code") == "MCP_RECONNECT" for event in events)
    assert any(
        "TEST_ATTEMPT_1" in ref for event in events for ref in event.get("evidence_refs", [])
    )
    for event in events:
        contracts.validate_trace(event, "retry trace")


def test_initialization_retry_keeps_receive_first(tmp_path, monkeypatch):
    args = setup(tmp_path, monkeypatch, failure=httpx2.ConnectError("connect"), during="connect")
    asyncio.run(runner.solve_with_reconnect(*args[:4]))
    assert len(args[4]) == 2
    events = [json.loads(line) for line in args[3].path.read_text().splitlines()]
    assert events[0]["event_type"] == "case_received"


def test_retry_exhaustion_is_bounded_without_finalization(tmp_path, monkeypatch):
    args = setup(tmp_path, monkeypatch, failure=httpx2.ReadError("read"), always=True)
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        asyncio.run(runner.solve_with_reconnect(*args[:4]))
    assert len(args[4]) == 3 and args[5] == [1, 2]
    assert '"event_type":"case_finalized"' not in args[3].path.read_text()


@pytest.mark.parametrize("failure", [RuntimeError("403 Forbidden"), ContractError("extra field")])
def test_forbidden_and_contract_errors_fail_without_retry(tmp_path, monkeypatch, failure):
    args = setup(tmp_path, monkeypatch, failure=failure)
    with pytest.raises(EvidenceError):
        asyncio.run(runner.solve_with_reconnect(*args[:4]))
    assert len(args[4]) == 1 and args[5] == []


def test_transport_failure_after_verification_does_not_duplicate_finalization(
    tmp_path, monkeypatch
):
    args = setup(tmp_path, monkeypatch, failure=httpx2.ReadError("closing"), during="close")
    output = asyncio.run(runner.solve_with_reconnect(*args[:4]))
    assert output["assessment"]["primary_issue"] == "valid_split_payment"
    assert len(args[4]) == 1
    events = [json.loads(line) for line in args[3].path.read_text().splitlines()]
    assert sum(event["event_type"] == "case_finalized" for event in events) == 1
