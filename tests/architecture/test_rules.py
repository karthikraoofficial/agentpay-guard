"""Fails CI when a CLAUDE.md non-negotiable is broken. The fixture tests prove each rule
fires on code that breaks it and stays quiet on code that doesn't, so the repo checks
can't pass vacuously."""

from pathlib import Path

from tests.architecture.rules import environment_reads, model_gateway_in_policy, vendor_imports

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"
CODE_DIRS = ["api", "core", "adapters", "workflows", "agents", "evals", "migrations"]
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
