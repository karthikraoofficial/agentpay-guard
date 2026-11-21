"""Run the MCP server over stdio: `uv run python -m guard_mcp`.

An MCP host launches it as a local process. stdout carries the protocol, so every log
line goes to stderr. Exits 1, before serving anything, if its key isn't decide-only.
"""

import logging
import sys

import anyio
import httpx
from mcp.server.stdio import stdio_server

from core.settings import McpSettings
from guard_mcp.guard import KeyRefused
from guard_mcp.http import api_client
from guard_mcp.server import start


async def serve(settings: McpSettings, transport: httpx.AsyncBaseTransport | None = None) -> int:
    """Serve until the host closes stdin: 0. Refused at startup: 1, with the reason on
    stderr. `transport` is for tests."""
    async with api_client(settings, transport) as http:
        try:
            server = await start(http)
        except KeyRefused as refused:
            print(f"agentpay-guard MCP server: {refused}", file=sys.stderr)
            return 1
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    return 0


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(name)s %(message)s")
    # One line per tool call comes from guard_mcp.server; httpx's own would repeat each URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return anyio.run(serve, McpSettings())  # required fields come from the environment


if __name__ == "__main__":
    sys.exit(main())
