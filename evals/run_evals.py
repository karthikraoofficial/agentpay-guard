"""Score the policy engine against the labelled golden set.

`uv run python -m evals.run_evals` runs every case in a golden file (default
evals/golden/phase1.jsonl) through `decide()` and prints the false-allow and false-block
counts. It exits 1 on any mismatch, including a wrong Reason code, so CI fails.

A false-allow is an allow where the label says step_up or block; a false-block is a block
where the label says allow or step_up. Each case lists only what it changes from the
file's base (`<name>_base.json` beside `<name>.jsonl`). The base names the Policy pack
the file is decided with, so a golden file keeps testing the pack it was labelled for
after a newer version becomes active. The schema is provisional until the Evals PRD.
"""

import argparse
import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from core.domain.decisions import AgentPaymentRequest, Outcome, ReasonCode
from core.domain.mandates import Mandate
from core.domain.money import Money
from core.domain.parties import Agent, AgentStatus
from core.domain.signals import Signals
from core.policy.engine import decide
from core.policy.packs import load_pack

GOLDEN = Path(__file__).resolve().parent / "golden"
PACKS = Path(__file__).resolve().parents[1] / "policy_packs"


class Expected(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Outcome
    reason_codes: tuple[ReasonCode, ...]


class PackPin(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    version: str


class Case(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    description: str
    pack: PackPin
    mandate: dict[str, Any]
    request: dict[str, Any]
    agent_status: AgentStatus | None
    spent_this_month_minor: int
    now: datetime
    expected: Expected


class Mismatch(BaseModel):
    case_id: str
    description: str
    expected: Expected
    actual: Expected


class Report(BaseModel):
    total: int
    false_allow: int
    false_block: int
    mismatches: list[Mismatch]


def _merge(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_cases(path: Path) -> list[Case]:
    base = json.loads((path.parent / f"{path.stem}_base.json").read_text(encoding="utf-8"))
    base.pop("_about", None)
    lines = path.read_text(encoding="utf-8").splitlines()
    return [Case.model_validate(_merge(base, json.loads(line))) for line in lines if line.strip()]


def _actual(case: Case, pack_root: Path) -> Expected:
    pack = load_pack(pack_root, case.pack.id, case.pack.version).pack
    mandate = Mandate.model_validate(case.mandate)
    request = AgentPaymentRequest.model_validate(case.request)
    agent = (
        None
        if case.agent_status is None
        else Agent(
            agent_id=request.agent_id,
            tenant_id=mandate.tenant_id,
            name="Eval agent",
            status=case.agent_status,
            created_at=mandate.created_at,
        )
    )
    spent = Money(minor=case.spent_this_month_minor, currency=mandate.currency)
    output = decide(request, mandate, agent, spent, Signals(), pack, case.now)
    return Expected(outcome=output.outcome, reason_codes=output.reason_codes)


def run_cases(cases: Sequence[Case], pack_root: Path = PACKS) -> Report:
    mismatches: list[Mismatch] = []
    false_allow = false_block = 0
    for case in cases:
        actual = _actual(case, pack_root)
        if actual == case.expected:
            continue
        false_allow += actual.outcome == "allow" and case.expected.outcome != "allow"
        false_block += actual.outcome == "block" and case.expected.outcome != "block"
        mismatches.append(
            Mismatch(
                case_id=case.id, description=case.description, expected=case.expected, actual=actual
            )
        )
    return Report(
        total=len(cases), false_allow=false_allow, false_block=false_block, mismatches=mismatches
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.run_evals")
    parser.add_argument("--cases", type=Path, default=GOLDEN / "phase1.jsonl")
    parser.add_argument("--packs", type=Path, default=PACKS)
    args = parser.parse_args(argv)

    report = run_cases(load_cases(args.cases), args.packs)
    print(f"cases: {report.total}")
    print(f"false-allow: {report.false_allow}")
    print(f"false-block: {report.false_block}")
    print(f"mismatches: {len(report.mismatches)}")
    for m in report.mismatches:
        print(
            f"  {m.case_id} ({m.description}): expected {m.expected.outcome} "
            f"{list(m.expected.reason_codes)}, got {m.actual.outcome} {list(m.actual.reason_codes)}"
        )
    return 1 if report.mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
