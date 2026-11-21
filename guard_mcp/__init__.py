"""The AgentPay Guard MCP server (#242): a stdio thin client over the HTTP API, holding
exactly one `decide` key, so an Agent in an MCP host can ask for a Decision before it
pays. Every rule the API enforces (auth, the Tenant, idempotency, the Audit record, the
Decision itself) is left to the API (ADR-0029)."""
