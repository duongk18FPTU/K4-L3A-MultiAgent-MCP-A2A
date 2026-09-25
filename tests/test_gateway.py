from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway, GatewayHTTPError, check_http_response


def test_mcp_v2_discovery_pagination_and_cache():
    class Session:
        def __init__(self):
            self.pages = []

        async def list_tools(self, *, params=None):
            cursor = params.cursor if params else None
            self.pages.append(cursor)
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="second" if cursor else "first",
                        description="read",
                        input_schema={"type": "object"},
                    )
                ],
                next_cursor=None if cursor else "page-2",
            )

    async def check():
        session = Session()
        gateway = EvidenceGateway(session, None)
        assert await gateway.list_tools() == ["first", "second"]
        assert set(await gateway.describe_tools()) == {"first", "second"}
        assert session.pages == [None, "page-2"]

    asyncio.run(check())


def test_mcp_v2_call_preserves_reference_and_passes_scope():
    evidence = {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_UNIT_TEST_ONLY_GATEWAY_000000",
        "result_hash": "sha256:" + "0" * 64,
        "domain": "order",
        "data": {"order_id": "order-1"},
    }

    class Session:
        async def call_tool(self, name, *, arguments):
            assert name == "get_order"
            assert arguments == {"case_id": "TEST_CASE_001", "order_id": "order-1"}
            return SimpleNamespace(is_error=False, structured_content=evidence)

    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts/schemas")
    gateway = EvidenceGateway(Session(), contracts)
    assert (
        asyncio.run(gateway.call("get_order", case_id="TEST_CASE_001", order_id="order-1"))
        == evidence
    )


def test_tool_error_is_not_evidence():
    class Session:
        async def call_tool(self, *args, **kwargs):
            return SimpleNamespace(is_error=True, content=[SimpleNamespace(text="403 Forbidden")])

    with pytest.raises(RuntimeError, match="403"):
        asyncio.run(EvidenceGateway(Session(), None).call("get_order", case_id="TEST_CASE_001"))


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503, 504])
def test_http_hook_preserves_status_without_leaking_body(status):
    response = httpx2.Response(
        status,
        text="private details",
        request=httpx2.Request(
            "POST",
            "https://example.invalid/mcp",
            headers={"Authorization": "Bearer secret"},
        ),
    )
    with pytest.raises(GatewayHTTPError) as info:
        asyncio.run(check_http_response(response))
    assert info.value.status_code == status
    assert str(info.value) == f"MCP gateway returned HTTP {status}"


def test_http_hook_does_not_consume_success_stream():
    response = httpx2.Response(
        200,
        text="event: message",
        request=httpx2.Request(
            "POST",
            "https://example.invalid/mcp",
        ),
    )
    asyncio.run(check_http_response(response))
    assert response.text == "event: message"


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_optional_transport_endpoint_405_is_left_to_sdk(method):
    response = httpx2.Response(405, request=httpx2.Request(method, "https://example.invalid/mcp"))
    asyncio.run(check_http_response(response))
