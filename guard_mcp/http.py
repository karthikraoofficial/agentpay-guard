"""The MCP server's one HTTP client: the API base URL, the key as the Bearer token, and
the timeout, all from McpSettings."""

import httpx

from core.settings import McpSettings


def api_client(
    settings: McpSettings, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """`transport` is for tests, which send to the app in-process instead of the network."""
    return httpx.AsyncClient(
        base_url=str(settings.api_base_url),
        headers={"Authorization": f"Bearer {settings.api_key.get_secret_value()}"},
        timeout=settings.http_timeout_seconds,
        transport=transport,
    )
