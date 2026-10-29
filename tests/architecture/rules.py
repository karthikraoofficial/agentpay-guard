"""Static checks for CLAUDE.md non-negotiables. Sources are parsed, never imported or run."""

import ast
from dataclasses import dataclass
from pathlib import Path

# Non-negotiable 2. The one place to add a vendor: these packages never appear in core/.
VENDOR_MODULES = frozenset(
    {
        "anthropic",
        "fastapi",
        "httpx",
        "openai",
        "opentelemetry",
        "psycopg",
        "psycopg2",
        "razorpay",
        "redis",
        "requests",
        "sqlalchemy",
        "starlette",
        "stripe",
        "temporalio",
        "uvicorn",
    }
)

ENV_READERS = frozenset({"environ", "getenv", "environb", "getenvb"})


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.detail}"


def python_files(root: Path, *dirs: str) -> list[Path]:
    return sorted(p for d in dirs if (root / d).is_dir() for p in (root / d).rglob("*.py"))


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def vendor_imports(root: Path) -> list[Violation]:
    """Every import of a vendor package from a module under core/."""
    found: list[Violation] = []
    for path in python_files(root, "core"):
        for node in ast.walk(_tree(path)):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in VENDOR_MODULES:
                    found.append(Violation(path.relative_to(root), node.lineno, f"imports {name}"))
    return found


def environment_reads(root: Path, code_dirs: list[str], settings_module: str) -> list[Violation]:
    """Every os.environ / os.getenv use outside the settings module (non-negotiable 9)."""
    allowed = (root / settings_module).resolve()
    found: list[Violation] = []
    for path in python_files(root, *code_dirs):
        if path.resolve() == allowed:
            continue
        for node in ast.walk(_tree(path)):
            hit = (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
                and node.attr in ENV_READERS
            ) or (
                isinstance(node, ast.ImportFrom)
                and node.module == "os"
                and any(alias.name in ENV_READERS for alias in node.names)
            )
            if hit:
                found.append(
                    Violation(path.relative_to(root), node.lineno, "reads the environment")
                )
    return found


def model_gateway_in_policy(root: Path) -> list[Violation]:
    """Any reference to ModelGateway under core/policy (non-negotiable 1)."""
    found: list[Violation] = []
    for path in python_files(root, "core/policy"):
        for node in ast.walk(_tree(path)):
            name = node.id if isinstance(node, ast.Name) else getattr(node, "attr", None)
            if name == "ModelGateway" or (
                isinstance(node, ast.alias) and node.name == "ModelGateway"
            ):
                found.append(
                    Violation(
                        path.relative_to(root),
                        getattr(node, "lineno", 0),
                        "references ModelGateway",
                    )
                )
    return found


# ADR-0003. The engine and the pack model may import only these; nothing that reads a
# clock, a store, a gateway, files or the network.
ENGINE_MODULES = ("core/policy/engine.py", "core/policy/pack.py")
ENGINE_ALLOWED_IMPORTS = (
    "collections",
    "dataclasses",
    "datetime",
    "enum",
    "functools",
    "hashlib",
    "typing",
    "pydantic",
    "core.domain",
    "core.policy.pack",
)
CLOCK_CALLS = frozenset({"now", "utcnow", "today", "time", "monotonic", "perf_counter"})


def impure_engine(root: Path) -> list[Violation]:
    """Imports outside the allow-list, and clock reads, in the policy engine."""
    found: list[Violation] = []
    for rel in ENGINE_MODULES:
        path = root / rel
        if not path.is_file():
            continue
        for node in ast.walk(_tree(path)):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                if not any(name == a or name.startswith(a + ".") for a in ENGINE_ALLOWED_IMPORTS):
                    found.append(Violation(Path(rel), node.lineno, f"imports {name}"))
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in CLOCK_CALLS
            ):
                found.append(Violation(Path(rel), node.lineno, f"calls .{node.func.attr}()"))
    return found
