"""Fails CI when a CLAUDE.md non-negotiable is broken. The fixture tests prove each rule
fires on code that breaks it and stays quiet on code that doesn't, so the repo checks
can't pass vacuously."""

from pathlib import Path

from tests.architecture.rules import (
    environment_reads,
    imports_from,
    impure_engine,
    model_gateway_in_policy,
    vendor_imports,
    vendors_outside_their_adapter,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"
CODE_DIRS = [
    "api",
    "core",
    "adapters",
    "workflows",
    "agents",
    "evals",
    "migrations",
    "guard_mcp",
    "demo",
    "demo_runner",
]
# The MCP server is a thin client over the HTTP API (#242): it never decides or stores
# anything itself, so it imports none of these.
MCP_FORBIDDEN = ("core.policy", "api", "adapters", "workflows", "agents", "evals")
# The demo's step engine and the Demo runner are HTTP clients of the API too (#273,
# ADR-0031): no model call of their own, no store, no engine.
DEMO_FORBIDDEN = (*MCP_FORBIDDEN, "anthropic")
SETTINGS = "core/settings.py"


def _report(violations: list[object]) -> str:
    return "\n".join(str(v) for v in violations)


# --- The repository itself ----------------------------------------------------


def test_core_imports_no_vendor_sdk() -> None:
    violations = vendor_imports(REPO)
    assert not violations, "Move vendor code to adapters/:\n" + _report(list(violations))


def test_environment_read_only_in_settings() -> None:
    violations = environment_reads(REPO, CODE_DIRS, SETTINGS)
    assert not violations, "Use core.settings instead:\n" + _report(list(violations))


def test_policy_never_references_model_gateway() -> None:
    violations = model_gateway_in_policy(REPO)
    assert not violations, "No LLM calls in core/policy:\n" + _report(list(violations))


def test_the_policy_engine_reads_no_clock_store_gateway_or_io() -> None:
    violations = impure_engine(REPO)
    assert not violations, "Pass values into decide() (ADR-0003):\n" + _report(list(violations))


def test_each_vendor_is_imported_only_by_its_own_adapter() -> None:
    violations = vendors_outside_their_adapter(REPO, CODE_DIRS)
    assert not violations, "Import this vendor only in its adapter:\n" + _report(list(violations))


# --- Proof the rules fire -----------------------------------------------------


def test_vendor_rule_flags_each_vendor_import() -> None:
    found = {(v.path.as_posix(), v.detail) for v in vendor_imports(FIXTURES / "vendor_import")}
    assert found == {
        ("core/repo.py", "imports sqlalchemy"),
        ("core/llm.py", "imports anthropic.types"),
    }


def test_environment_rule_flags_attribute_and_from_import_reads() -> None:
    found = {
        v.path.as_posix()
        for v in environment_reads(FIXTURES / "env_read", ["api", "core"], SETTINGS)
    }
    assert found == {"api/app.py", "core/util.py"}


def test_clean_fixture_passes_both_rules() -> None:
    clean = FIXTURES / "clean"
    assert vendor_imports(clean) == []
    assert environment_reads(clean, ["api", "core"], SETTINGS) == []


def test_engine_rule_flags_impure_imports_and_clock_reads() -> None:
    found = {(v.path.as_posix(), v.detail) for v in impure_engine(FIXTURES / "impure_engine")}
    assert found == {
        ("core/policy/engine.py", "imports core.ports"),
        ("core/policy/engine.py", "imports pathlib"),
        ("core/policy/engine.py", "calls .now()"),
    }


def test_vendor_home_rule_flags_imports_outside_the_adapter() -> None:
    root = FIXTURES / "vendor_home"
    found = {
        (v.path.as_posix(), v.detail)
        for v in vendors_outside_their_adapter(root, ["api", "adapters"])
    }
    assert found == {
        ("api/main.py", "imports opentelemetry.sdk.trace outside adapters/otel"),
        ("adapters/stray.py", "imports opentelemetry.trace outside adapters/otel"),
        ("api/llm.py", "imports anthropic outside adapters/claude"),
    }


def test_the_mcp_server_is_a_thin_client_and_core_never_imports_it() -> None:
    violations = imports_from(REPO, ["guard_mcp"], MCP_FORBIDDEN)
    violations += imports_from(REPO, ["core"], ("guard_mcp",))
    assert violations == [], _report(violations)


def test_the_demo_and_its_runner_only_talk_to_the_api() -> None:
    violations = imports_from(REPO, ["demo", "demo_runner"], DEMO_FORBIDDEN)
    violations += imports_from(REPO, ["core"], ("demo", "demo_runner"))
    assert violations == [], _report(violations)


def test_the_mcp_boundary_rule_flags_each_forbidden_import() -> None:
    root = FIXTURES / "mcp_boundary"
    found = {
        (v.path.as_posix(), v.detail) for v in imports_from(root, ["guard_mcp"], MCP_FORBIDDEN)
    }
    found |= {(v.path.as_posix(), v.detail) for v in imports_from(root, ["core"], ("guard_mcp",))}
    assert found == {
        ("guard_mcp/server.py", "imports core.policy.engine"),
        ("guard_mcp/server.py", "imports api.deps"),
        ("core/hook.py", "imports guard_mcp.tools"),
    }
