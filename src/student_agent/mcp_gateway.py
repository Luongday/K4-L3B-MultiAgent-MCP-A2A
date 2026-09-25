from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts

CALL_TIMEOUT_SECONDS = 60.0


class ToolCallError(RuntimeError):
    """The MCP server answered, but the tool reported an error (e.g. unknown order)."""


class GatewayUnavailable(RuntimeError):
    """The MCP transport failed or timed out; the session should be reconnected."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        self._tools: tuple[ToolSpec, ...] | None = None
        self.record_dir: Path | None = None

    async def discover_tools(self) -> tuple[ToolSpec, ...]:
        if self._tools is None:
            try:
                response = await asyncio.wait_for(self._session.list_tools(), CALL_TIMEOUT_SECONDS)
            except TimeoutError as exc:
                raise GatewayUnavailable("MCP tool discovery timed out") from exc
            self._tools = tuple(
                sorted(
                    (
                        ToolSpec(
                            name=tool.name,
                            description=tool.description or "",
                            input_schema=tool.input_schema or {},
                        )
                        for tool in response.tools
                    ),
                    key=lambda item: item.name,
                )
            )
        return self._tools

    async def list_tools(self) -> list[str]:
        return [tool.name for tool in await self.discover_tools()]

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        if "case_id" in arguments:
            raise ValueError("case_id must be supplied only through the case_id parameter")
        if tool_name not in {tool.name for tool in await self.discover_tools()}:
            raise ValueError(f"MCP tool was not discovered: {tool_name}")
        payload = {"case_id": case_id, **arguments}
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(tool_name, arguments=payload), CALL_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            raise GatewayUnavailable(f"MCP tool {tool_name} timed out") from exc
        except Exception as exc:  # transport errors surface as many exception types
            raise GatewayUnavailable(
                f"MCP transport failed during {tool_name}: {type(exc).__name__}"
            ) from exc
        if result.is_error:
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            self._record(case_id, tool_name, arguments, {"error": message or "unknown error"})
            raise ToolCallError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            evidence = getattr(result, "structured_content", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        self._record(case_id, tool_name, arguments, evidence)
        return evidence

    def _record(
        self, case_id: str, tool_name: str, arguments: dict[str, Any], response: Any
    ) -> None:
        """Keep raw responses locally so analysis can be revisited without new MCP calls."""
        if self.record_dir is None:
            return
        self.record_dir.mkdir(parents=True, exist_ok=True)
        entry = {"tool_name": tool_name, "arguments": arguments, "response": response}
        with (self.record_dir / f"{case_id}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


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
