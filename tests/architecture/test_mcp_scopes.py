"""No MCP tool may reach a `consent` or `manage` route (ADR-0011, #242). Each registered
tool is resolved to its route in the real FastAPI app, and that route's dependency tree
is searched for the consent or manage scope check."""

from collections.abc import Iterator
from typing import Any, get_args

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from adapters.memory.unit_of_work import InMemoryUnitOfWork
from api.deps import ConsentTenant, ManageTenant
from api.main import create_app
from guard_mcp.tools import TOOLS, Tool
from tests.conftest import make_settings

FORBIDDEN = {
    get_args(ConsentTenant)[1].dependency: "consent",
    get_args(ManageTenant)[1].dependency: "manage",
}


def _flatten(routes: list[Any]) -> Iterator[APIRoute]:
    # FastAPI 0.164 wraps each included router; ours set their prefix on the APIRouter
    # itself, so the wrapped routes already carry their full path.
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif (inner := getattr(route, "original_router", None)) is not None:
            yield from _flatten(inner.routes)


@pytest.fixture(scope="module")
def routes() -> list[APIRoute]:
    found = list(_flatten(create_app(make_settings(), store=InMemoryUnitOfWork()).routes))
    assert any(r.path == "/v1/decisions" for r in found)  # the walk reached the routers
    return found


def _route(routes: list[APIRoute], tool: Tool) -> APIRoute:
    found = [r for r in routes if r.path == tool.path and tool.method in (r.methods or set())]
    assert len(found) == 1, f"{tool.name}: no single route for {tool.method} {tool.path}"
    return found[0]


def _calls(dependant: Dependant) -> Iterator[Any]:
    for dep in dependant.dependencies:
        yield dep.call
        yield from _calls(dep)


def forbidden_scopes(routes: list[APIRoute], tool: Tool) -> set[str]:
    return {FORBIDDEN[c] for c in _calls(_route(routes, tool).dependant) if c in FORBIDDEN}


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_no_tool_reaches_a_consent_or_manage_route(routes: list[APIRoute], tool: Tool) -> None:
    assert forbidden_scopes(routes, tool) == set()


@pytest.mark.parametrize(
    ("method", "path", "scope"),
    [
        ("POST", "/v1/consent-requests/{consent_request_id}/approve", "consent"),
        ("POST", "/v1/mandates", "manage"),
    ],
)
def test_the_check_flags_a_consent_or_manage_route(
    routes: list[APIRoute], method: str, path: str, scope: str
) -> None:
    rogue = Tool(name="rogue", description="", method=method, path=path)  # type: ignore[arg-type]
    assert forbidden_scopes(routes, rogue) == {scope}
