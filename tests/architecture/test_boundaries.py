"""Turns CLAUDE.md non-negotiables into failing tests, so a shortcut breaks CI rather than
slipping through review."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CODE_DIRS = ["api", "core", "adapters", "workflows", "agents", "evals"]

# Non-negotiable 2: vendor SDKs and frameworks stay out of core/.
VENDOR_MODULES = {
    "anthropic",
    "fastapi",
    "httpx",
    "opentelemetry",
    "psycopg",
    "razorpay",
    "requests",
    "sqlalchemy",
    "starlette",
    "stripe",
    "temporalio",
}


def _python_files(*dirs: str) -> list[Path]:
    return sorted(p for d in dirs for p in (ROOT / d).rglob("*.py"))


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("path", _python_files("core"), ids=lambda p: str(p.relative_to(ROOT)))
def test_core_imports_no_vendor_sdk(path: Path) -> None:
    leaked = _imported_roots(path) & VENDOR_MODULES
    assert not leaked, f"{path.relative_to(ROOT)} imports {sorted(leaked)}; move it to adapters/"


def test_policy_never_imports_model_gateway() -> None:
    """Non-negotiable 1: no LLM call inside core/policy."""
    for path in _python_files("core/policy"):
        source = path.read_text(encoding="utf-8")
        assert "ModelGateway" not in source, f"{path.relative_to(ROOT)} references ModelGateway"


def test_environment_read_only_in_settings() -> None:
    """Non-negotiable 9: all config through core/settings.py."""
    allowed = ROOT / "core" / "settings.py"
    for path in _python_files(*CODE_DIRS):
        if path == allowed:
            continue
        source = path.read_text(encoding="utf-8")
        assert "os.environ" not in source and "os.getenv" not in source, (
            f"{path.relative_to(ROOT)} reads the environment; use core.settings.get_settings()"
        )
