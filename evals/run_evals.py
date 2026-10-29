"""Eval runner and CI gate.

Phase 0 stub: it finds the golden set and exits cleanly while it is empty. It fails
once cases exist, so the first PR that adds golden cases must also add scoring
(false-allow, false-block, step-up rate, p95 latency). Release thresholds arrive
with the Evals PRD.
"""

import sys
from pathlib import Path

GOLDEN_DIR = Path(__file__).parent / "golden"


def main() -> int:
    cases = sorted(GOLDEN_DIR.glob("*.jsonl"))
    if not cases:
        print("evals: no golden cases yet; gate passes trivially")
        return 0
    print(f"evals: found {len(cases)} golden file(s), but scoring is not implemented yet")
    return 1


if __name__ == "__main__":
    sys.exit(main())
