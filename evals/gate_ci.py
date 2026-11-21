"""CI's Release gate step: run the gate on pull requests that could change which Policy
pack decides.

A pull request triggers it when its diff adds a pack file or a declarations file, or
changes the Manifest. Every pack version the pull request adds, and the Manifest's new
active version if it moves, is gated against the active version on the base branch, so a
pack released inactive is still checked. Usage:

    python -m evals.gate_ci <base-commit> [--summary <file>]

`--summary` appends the Markdown summary to a file (CI passes `$GITHUB_STEP_SUMMARY`).
Exit 0 when every gate passes or none is needed, 1 when one fails, 2 on an input error.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

from pydantic import ValidationError

from core.policy.packs import Manifest, PackError, load_manifest
from evals.release_gate import (
    DeclarationsError,
    load_declarations,
    print_report,
    run_gate,
    summary_markdown,
)
from evals.run_evals import GOLDEN, PACKS, PackPin

_ADDED_PACK_OR_DECLARATIONS = re.compile(r"^policy_packs/[^/]+/[^/]+\.yaml$")
_MANIFEST = "policy_packs/manifest.json"


def triggered(name_status: str) -> bool:
    """Whether a pull request's `git diff --name-status` calls for the Release gate."""
    for line in name_status.splitlines():
        status, *paths = line.split("\t")
        if _MANIFEST in paths:
            return True
        if status == "A" and _ADDED_PACK_OR_DECLARATIONS.match(paths[0]):
            return True
    return False


def gates(name_status: str, base: Manifest, head: Manifest) -> list[tuple[PackPin, PackPin]]:
    """The (active, candidate) pins to gate: every version the head Manifest adds, and its
    active version if it moved, each against the base branch's active version."""
    if not triggered(name_status):
        return []
    pairs: list[tuple[PackPin, PackPin]] = []
    for pack_id, entry in head.packs.items():
        before = base.packs.get(pack_id)
        # A pack id new on this branch has no baseline: gate it against itself, so its
        # own golden files are still checked.
        baseline = before.active if before else entry.active
        candidates = [v for v in entry.versions if not before or v not in before.versions]
        if before and entry.active != before.active and entry.active not in candidates:
            candidates.append(entry.active)
        pairs += [
            (PackPin(id=pack_id, version=baseline), PackPin(id=pack_id, version=v))
            for v in candidates
        ]
    return pairs


def run(name_status: str, base: Manifest, packs: Path, golden: Path, summary: Path | None) -> int:
    """Gate what the diff calls for, print each report and write the job summary."""
    parts: list[str] = []
    code = 0
    try:
        pairs = gates(name_status, base, load_manifest(packs))
        if not pairs:
            print("Release gate: skipped, no new pack version or Manifest change.")
            parts.append("### Release gate: skipped, no new pack version or Manifest change.\n")
        for active, candidate in pairs:
            declared = load_declarations(packs, candidate)
            verdict = run_gate(packs, golden, active, candidate, declared)
            print_report(active, candidate, verdict)
            parts.append(summary_markdown(active, candidate, verdict))
            code = max(code, 0 if verdict.passed else 1)
    except (PackError, DeclarationsError) as e:
        print(f"release gate: {e}", file=sys.stderr)
        parts.append(f"### Release gate: input error\n\n```\n{e}\n```\n")
        code = 2
    if summary is not None:
        with summary.open("a", encoding="utf-8") as f:
            f.write("\n".join(parts))
    return code


def _git(*args: str) -> str:
    # A fixed git command; the only argument from outside is CI's base ref.
    command = ["git", *args]
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout  # noqa: S603


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.gate_ci")
    parser.add_argument("base", help="the base branch's commit")
    parser.add_argument("--summary", type=Path, help="append the Markdown summary here")
    args = parser.parse_args(argv)
    diff = _git("diff", "--name-status", args.base, "HEAD", "--", "policy_packs")
    try:
        base = Manifest.model_validate_json(_git("show", f"{args.base}:{_MANIFEST}"))
    except ValidationError as e:
        print(f"release gate: the base branch's Manifest can't be read: {e}", file=sys.stderr)
        return 2
    return run(diff, base, PACKS, GOLDEN, args.summary)


if __name__ == "__main__":
    raise SystemExit(main())
