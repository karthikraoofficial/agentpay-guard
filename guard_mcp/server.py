"""The MCP server: lists the tools and turns each tool call into one API call.

Results are the API's JSON body, as structured content and as text. An API error
becomes a tool error that starts with the API's error code, so an Agent can tell a
refusal from an outage. Each call is logged with its tool name, HTTP status, Outcome
and latency only: never the key, the arguments or the response body.
"""

import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any
from urllib.parse import quote

import httpx
import mcp_types as types
from mcp.server import Server, ServerRequestContext

from guard_mcp.guard import check_key
from guard_mcp.tools import IDEMPOTENCY_KEY, STEP_UP_NOTE, TOOLS, Tool

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "AgentPay Guard decides whether an Agent may make a payment. Call request_decision "
    "before every payment and do what its outcome says. Nothing here can approve a "
    "step_up: only the Payer can."
)


async def start(http: httpx.AsyncClient) -> Server[Any]:
    """The server, once the startup key guard has passed. Raises KeyRefused otherwise."""
    await check_key(http)
    return build_server(http)


def build_server(http: httpx.AsyncClient, tools: Sequence[Tool] = TOOLS) -> Server[Any]:
    by_name = {tool.name: tool for tool in tools}

    async def list_tools(
        _ctx: ServerRequestContext[Any], _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(name=t.name, description=t.description, input_schema=t.input_schema())
                for t in tools
            ]
        )

    async def call_tool(
        _ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        tool = by_name.get(params.name)
        if tool is None:
            return _error("UNKNOWN_TOOL", f"There is no tool {params.name}.")
        return await call(http, tool, params.arguments or {})

    return Server(
        "agentpay-guard",
        version=version("agentpay-guard"),
        instructions=INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def call(
    http: httpx.AsyncClient, tool: Tool, arguments: Mapping[str, Any]
) -> types.CallToolResult:
    """One tool call: one API call, logged."""
    started = time.perf_counter()
    status: int | None = None
    outcome: str | None = None
    try:
        request = _request(tool, arguments)
        if isinstance(request, types.CallToolResult):
            return request
        response = await http.request(
            tool.method, request.path, json=request.body, headers=request.headers
        )
        status = response.status_code
        result, outcome = _result(response)
        return result
    except httpx.HTTPError as error:
        return _error(
            "API_UNREACHABLE",
            f"AgentPay Guard didn't answer ({type(error).__name__}). Retry with the same "
            "idempotency key; don't pay without a Decision.",
        )
    finally:
        log.info(
            "tool=%s status=%s outcome=%s latency_ms=%.1f",
            tool.name,
            status if status is not None else "-",
            outcome or "-",
            (time.perf_counter() - started) * 1000,
        )


@dataclass(frozen=True)
class _Request:
    path: str
    body: dict[str, Any] | None
    headers: dict[str, str]


def _request(tool: Tool, arguments: Mapping[str, Any]) -> _Request | types.CallToolResult:
    """The API call a tool call makes, or the tool error for arguments it can't send."""
    args = dict(arguments)
    path = tool.path
    for name in tool.path_params:
        value = args.pop(name, None)
        if not isinstance(value, str) or not value:
            return _error("INVALID_ARGUMENTS", f"{tool.name} needs {name} as a string.")
        # Quoted whole, so a value can't climb out of its path segment.
        path = path.replace("{" + name + "}", quote(value, safe=""))
    headers: dict[str, str] = {}
    if not tool.writes:
        return _Request(path, None, headers)
    key = args.pop(IDEMPOTENCY_KEY, None)
    if isinstance(key, str) and key:
        headers["Idempotency-Key"] = key
    # Without a key the API answers IDEMPOTENCY_KEY_MISSING, which becomes the tool error.
    return _Request(path, args, headers)


def _result(response: httpx.Response) -> tuple[types.CallToolResult, str | None]:
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return _error(f"HTTP_{response.status_code}", "The API's answer wasn't JSON."), None
    if response.is_success:
        outcome = body.get("outcome")
        text = json.dumps(body, sort_keys=True)
        if outcome == "step_up":
            text = f"{STEP_UP_NOTE}\n\n{text}"
        content = [types.TextContent(type="text", text=text)]
        return types.CallToolResult(content=content, structured_content=body), outcome
    code = body.get("code") or f"HTTP_{response.status_code}"
    detail = body.get("detail") or ""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        detail = f"{detail} Retry after {retry_after} seconds."
    return _error(code, detail, body), None


def _error(code: str, detail: str, body: dict[str, Any] | None = None) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"{code}: {detail}")],
        structured_content=body if body is not None else {"code": code, "detail": detail},
        is_error=True,
    )
