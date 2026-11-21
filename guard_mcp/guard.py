"""The startup key guard: the server serves nothing unless its key holds exactly `decide`.

A key that also holds `consent` would let a prompt-injected Agent approve its own
step_up, and `manage` would let it widen its own Mandate (ADR-0011). Keys made before
scopes existed hold all three, so they are refused too.
"""

import httpx

HOW_TO_FIX = (
    "Issue a key with only the decide scope and set it as AGENTPAY_MCP_API_KEY: "
    "uv run python -m api.admin create-key --tenant {tenant} --scopes decide"
)


class KeyRefused(Exception):
    """Why the server won't start. Never holds the key."""


async def check_key(http: httpx.AsyncClient) -> None:
    """Ask the API what the key may do (GET /v1/api-keys/self); raise KeyRefused unless
    the answer is exactly `decide`."""
    try:
        response = await http.get("/v1/api-keys/self")
    except httpx.HTTPError as error:
        raise KeyRefused(
            f"Couldn't reach AgentPay Guard at {http.base_url} ({type(error).__name__}). "
            "Check AGENTPAY_MCP_API_BASE_URL and that the API is running."
        ) from None
    if response.status_code == 401:
        raise KeyRefused(
            "The API refused AGENTPAY_MCP_API_KEY: it isn't a key it knows. "
            + HOW_TO_FIX.format(tenant="<tenant_id>")
        )
    if response.status_code != 200:
        raise KeyRefused(f"GET /v1/api-keys/self answered HTTP {response.status_code}.")
    details = response.json()
    scopes = sorted(details.get("scopes", []))
    if scopes != ["decide"]:
        raise KeyRefused(
            f"AGENTPAY_MCP_API_KEY holds the scopes {', '.join(scopes) or 'none'}. The MCP "
            "server needs a key with exactly the decide scope, so an Agent can't approve its "
            "own step_up or change its own Mandate (ADR-0011). "
            + HOW_TO_FIX.format(tenant=details.get("tenant_id", "<tenant_id>"))
        )
