"""CI's released-packs check: released Policy packs and their declarations files are
immutable (ADR-0002, ADR-0028).

Fails when a change modifies, renames or deletes a pack file or declarations file that
already exists on the base branch, or adds a declarations file without adding its pack
in the same change. Usage: python -m evals.released_packs <base-commit>
"""

import re
import subprocess
import sys

_PACK_FILE = re.compile(r"^policy_packs/[^/]+/[^/]+\.yaml$")
_DECLARATIONS = re.compile(r"^(policy_packs/[^/]+/[^/]+)\.declared-changes\.yaml$")


def violations(name_status: str) -> list[str]:
    """The lines of `git diff --name-status <base> HEAD` that break the rule."""
    rows = [line.split("\t") for line in name_status.splitlines() if line.strip()]
    added = {paths[0] for status, *paths in rows if status == "A"}
    bad: list[str] = []
    for status, *paths in rows:
        if not any(_PACK_FILE.match(p) for p in paths):
            continue
        if status != "A":
            bad.append("\t".join([status, *paths]))
            continue
        declarations = _DECLARATIONS.match(paths[0])
        if declarations and f"{declarations[1]}.yaml" not in added:
            bad.append("\t".join([status, *paths]))
    return bad


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m evals.released_packs <base-commit>", file=sys.stderr)
        return 2
    # A fixed git command; the one argument is CI's base ref, passed as a list item.
    command = ["git", "diff", "--name-status", args[0], "HEAD", "--", "policy_packs"]
    diff = subprocess.run(command, check=True, capture_output=True, text=True).stdout  # noqa: S603
    bad = violations(diff)
    if bad:
        print(
            "::error::Released policy packs and their declarations files are immutable, "
            "and a declarations file ships with its pack. Add a new version instead of "
            "these changes:"
        )
        print("\n".join(bad))
        return 1
    print("No released policy pack or declarations file was changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
