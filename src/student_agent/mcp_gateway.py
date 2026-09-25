from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import PaginatedRequestParams

from .contracts import Contracts


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        self._tool_catalog: dict[str, dict[str, Any]] | None = None

    async def list_tools(self) -> list[str]:
        return sorted(await self.describe_tools())

    async def describe_tools(self) -> dict[str, dict[str, Any]]:
        """Discover full input contracts, including paginated tool inventories."""
        if self._tool_catalog is not None:
            return self._tool_catalog
        tools = {}
        cursor = None
        seen_cursors = set()
        while True:
            response = await self._session.list_tools(
                params=PaginatedRequestParams(cursor=cursor) if cursor else None
            )
            for tool in response.tools:
                tools[tool.name] = {
                    "description": tool.description,
                    "inputSchema": tool.input_schema,
                }
            cursor = response.next_cursor
            if not cursor:
                self._tool_catalog = tools
                return tools
            if cursor in seen_cursors:
                raise ValueError("MCP discovery returned a repeated pagination cursor")
            seen_cursors.add(cursor)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        result = await self._session.call_tool(tool_name, arguments=payload)
        if result.is_error:
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise RuntimeError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            evidence = getattr(result, "structured_content", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        return evidence


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)
