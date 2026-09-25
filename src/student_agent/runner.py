"""Retry read-only investigations at the MCP session boundary, never in a dead session."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import anyio
import httpx2
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

from .config import Settings
from .contracts import Contracts
from .mcp_gateway import GatewayHTTPError, connect_gateway
from .trace import TraceWriter
from .workflow import EvidenceError, solve_case


def is_transient_transport(exc: BaseException) -> bool:
    """Unwrap TaskGroup failures, but never mask mixed transport/validation errors."""
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(is_transient_transport(e) for e in exc.exceptions)
    if isinstance(exc, EvidenceError) and exc.__cause__ is not None:
        return is_transient_transport(exc.__cause__)
    if isinstance(exc, httpx2.HTTPStatusError):
        return exc.response.status_code in {429, 500, 502, 503, 504}
    if isinstance(exc, GatewayHTTPError):
        return exc.status_code in {429, 500, 502, 503, 504}
    if isinstance(exc, MCPError):
        # -32000 is also used for generic server errors; never retry that code
        # alone (e.g. Forbidden). Recognize the SDK's explicit closed-channel error.
        return exc.code == REQUEST_TIMEOUT or (
            exc.code == CONNECTION_CLOSED and exc.message == "Connection closed"
        )
    return isinstance(
        exc,
        (
            httpx2.NetworkError,
            httpx2.TimeoutException,
            httpx2.RemoteProtocolError,
            TimeoutError,
            anyio.EndOfStream,
            anyio.BrokenResourceError,
            anyio.ClosedResourceError,
        ),
    )


async def solve_with_reconnect(
    case: dict[str, Any],
    settings: Settings,
    contracts: Contracts,
    trace: TraceWriter,
    *,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Retry the unfinished case with fresh evidence; do not touch completed cases.

    All workflow tools are reads. An interrupted request can still appear in the
    server audit, so retrying may add calls. We neither reuse partial findings nor
    erase already emitted events. Cancellation and non-transport failures propagate.
    """
    if not 1 <= max_attempts <= 5:
        raise ValueError("max_attempts must be between 1 and 5")
    investigation_started = False
    for attempt in range(1, max_attempts + 1):
        verified_output = None
        try:
            async with connect_gateway(
                settings.mcp_endpoint,
                settings.team_api_key,
                contracts,
            ) as gateway:
                investigation_started = True
                candidate = await solve_case(case, gateway, trace)
                contracts.validate_output(candidate, f"case {case['case_id']}")
                if candidate["case_id"] != case["case_id"]:
                    raise EvidenceError("Solver returned a different case_id")
                verified_output = candidate
            return verified_output
        except Exception as exc:
            if not is_transient_transport(exc):
                raise
            # A transport failure during session teardown cannot invalidate a fully
            # received, verified decision. Retrying it would duplicate finalization.
            if verified_output is not None:
                print(
                    f"MCP connection closed after {case['case_id']} was verified; "
                    "keeping the verified result.",
                    file=sys.stderr,
                )
                return verified_output
            if attempt == max_attempts:
                raise RuntimeError(
                    f"MCP transport failed for {case['case_id']} after {max_attempts} attempts. "
                    "Completed outputs are preserved; check the MCP server/network, "
                    "then continue with day09 run --resume in the same competition run."
                ) from exc
            delay = float(2 ** (attempt - 1))
            if investigation_started:
                trace.emit(
                    case_id=case["case_id"],
                    event_type="handoff",
                    actor="coordinator",
                    target="coordinator",
                    decision_code="MCP_RECONNECT",
                    attributes={
                        "attempt": attempt,
                        "next_attempt": attempt + 1,
                        "delay_seconds": delay,
                        "error_type": type(exc).__name__,
                    },
                )
            print(
                f"MCP transport interrupted for {case['case_id']}; "
                f"retry {attempt + 1}/{max_attempts} in {delay:g}s with a new session.",
                file=sys.stderr,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")
