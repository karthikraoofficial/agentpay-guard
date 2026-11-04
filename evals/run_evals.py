"""Score the policy engine against the labelled golden set.

`uv run python -m evals.run_evals` runs every golden file in evals/golden/ (or one, with
`--cases`) through `decide()` and prints the false-allow and false-block counts per
file, the step-up rate and the engine's p95. It exits 1 on any mismatch, including a
wrong Reason code, so CI fails. A full run also fails when the committed Eval report or
README eval table is stale; `--write-report` regenerates both (evals.report). Files named
`disputes*.jsonl` are scored by evals.disputes instead: network codes, Evidence strength
and missing evidence; and `narrative_checks*.jsonl` by the citation checker's verdict on
labelled model replies.

A false-allow is an allow where the label says step_up or block; a false-block is a block
where the label says allow or step_up. Each case lists only what it changes from the
file's base (`<name>_base.json` beside `<name>.jsonl`). The base names the Policy pack
the file is decided with, so a golden file keeps testing the pack it was labelled for
after a newer version becomes active. A case may name its build-plan `scenario`, which
the Eval report counts by; a scenario belongs to one golden file.

A case with `stage: approval` is the re-check when a Payer approves a step_up (ADR-0010):
its inputs are as they are at approval, and it also labels the Consent resolution. There
a false-allow is an approval that goes through where the label says blocked, and a
false-block is the reverse.
"""

import argparse
import json
import math
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from core.domain.consent import ResolutionStatus, approval_result
from core.domain.decisions import AgentPaymentRequest, Outcome, ReasonCode
from core.domain.mandates import Mandate
from core.domain.money import Money
from core.domain.parties import Agent, AgentStatus
from core.domain.signals import Signals
from core.policy.engine import decide
from core.policy.pack import PolicyPack
from core.policy.packs import load_pack
from evals import report as eval_report
from evals.disputes import (
    load_dispute_cases,
    load_narrative_cases,
    run_dispute_cases,
    run_narrative_cases,
)

GOLDEN = Path(__file__).resolve().parent / "golden"
PACKS = Path(__file__).resolve().parents[1] / "policy_packs"


class Expected(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Outcome
    reason_codes: tuple[ReasonCode, ...]
    # Only on approval cases: the Consent resolution the re-check gives.
    resolution: ResolutionStatus | None = None


class PackPin(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    version: str


class Case(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    description: str
    pack: PackPin
    stage: Literal["decision", "approval"] = "decision"
    # The build-plan scenario the case belongs to, if any; the Eval report counts by it.
    scenario: str | None = None
    mandate: dict[str, Any]
    request: dict[str, Any]
    agent_status: AgentStatus | None
    spent_this_month_minor: int
    now: datetime
    # The Signals the API would have worked out, as a value (ADR-0003). Empty for Phase 1.
    signals: dict[str, Any] = {}
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
    # Actual outcomes of decision-stage cases (approval cases carry a resolution instead).
    allow: int = 0
    step_up: int = 0
    block: int = 0
    # Mismatches where the outcome (and resolution) is right but the Reason codes aren't.
    wrong_reason: int = 0
    scenarios: dict[str, eval_report.OutcomeCounts] = {}
    # Wall time of each decide() call, in milliseconds. Printed, never stored.
    engine_ms: list[float] = []


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


def _actual(case: Case, pack: PolicyPack) -> tuple[Expected, float]:
    """The engine's answer for one case, and how long decide() took in milliseconds."""
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
    signals = Signals.model_validate(case.signals)
    start = time.perf_counter()
    output = decide(request, mandate, agent, spent, signals, pack, case.now)
    ms = (time.perf_counter() - start) * 1000
    resolution = approval_result(output) if case.stage == "approval" else None
    actual = Expected(
        outcome=output.outcome, reason_codes=output.reason_codes, resolution=resolution
    )
    return actual, ms


def run_cases(cases: Sequence[Case], pack_root: Path = PACKS) -> Report:
    mismatches: list[Mismatch] = []
    false_allow = false_block = wrong_reason = 0
    outcomes = {"allow": 0, "step_up": 0, "block": 0}
    scenarios: dict[str, dict[str, int]] = {}
    engine_ms: list[float] = []
    packs: dict[PackPin, PolicyPack] = {}
    for case in cases:
        if case.pack not in packs:
            packs[case.pack] = load_pack(pack_root, case.pack.id, case.pack.version).pack
        actual, ms = _actual(case, packs[case.pack])
        engine_ms.append(ms)
        if case.stage == "decision":
            outcomes[actual.outcome] += 1
            if case.scenario is not None:
                counts = scenarios.setdefault(
                    case.scenario, {"cases": 0, "allow": 0, "step_up": 0, "block": 0}
                )
                counts["cases"] += 1
                counts[actual.outcome] += 1
        if actual == case.expected:
            continue
        wrong_reason += (actual.outcome, actual.resolution) == (
            case.expected.outcome,
            case.expected.resolution,
        )
        if case.stage == "approval":
            false_allow += actual.resolution == "approved" != case.expected.resolution
            false_block += actual.resolution == "blocked" != case.expected.resolution
        else:
            false_allow += actual.outcome == "allow" and case.expected.outcome != "allow"
            false_block += actual.outcome == "block" and case.expected.outcome != "block"
        mismatches.append(
            Mismatch(
                case_id=case.id, description=case.description, expected=case.expected, actual=actual
            )
        )
    return Report(
        total=len(cases),
        false_allow=false_allow,
        false_block=false_block,
        mismatches=mismatches,
        wrong_reason=wrong_reason,
        scenarios={k: eval_report.OutcomeCounts(**v) for k, v in scenarios.items()},
        engine_ms=engine_ms,
        **outcomes,
    )


def p95(samples: Sequence[float]) -> float:
    """Nearest-rank 95th percentile."""
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.run_evals")
    parser.add_argument("--cases", type=Path, help="one golden file (default: all of them)")
    parser.add_argument("--packs", type=Path, default=PACKS)
    parser.add_argument("--golden", type=Path, default=GOLDEN, help=argparse.SUPPRESS)
    parser.add_argument("--report", type=Path, default=eval_report.REPORT, help=argparse.SUPPRESS)
    parser.add_argument("--readme", type=Path, default=eval_report.README, help=argparse.SUPPRESS)
    parser.add_argument(
        "--write-report",
        action="store_true",
        help="write the Eval report and the README eval table from this run",
    )
    args = parser.parse_args(argv)
    if args.cases and args.write_report:
        parser.error("--write-report needs the whole golden set; drop --cases")

    files = [args.cases] if args.cases else sorted(args.golden.glob("*.jsonl"))
    entries: list[eval_report.FileEntry] = []
    scenarios: dict[str, eval_report.OutcomeCounts] = {}
    engine_ms: list[float] = []
    for path in files:
        if path.stem.startswith("disputes"):
            entries.append(_disputes(path, args.packs))
            continue
        if path.stem.startswith("narrative_checks"):
            entries.append(_narratives(path))
            continue
        cases = load_cases(path)
        report = run_cases(cases, args.packs)
        engine_ms += report.engine_ms
        for name, c in report.scenarios.items():
            if name in scenarios:
                raise SystemExit(f"scenario {name!r} is used in more than one golden file")
            scenarios[name] = c
        pack = f"{cases[0].pack.id}@{cases[0].pack.version}" if cases else "-"
        print(f"{path.name} ({pack})")
        print(f"  cases: {report.total}")
        print(f"  false-allow: {report.false_allow}")
        print(f"  false-block: {report.false_block}")
        print(f"  wrong reason: {report.wrong_reason}")
        print(f"  mismatches: {len(report.mismatches)}")
        for m in report.mismatches:
            print(
                f"    {m.case_id} ({m.description}): expected {m.expected.outcome} "
                f"{list(m.expected.reason_codes)}, got {m.actual.outcome} "
                f"{list(m.actual.reason_codes)}"
            )
        # A file of approval re-checks only has no outcomes to count (ADR-0010).
        decided = report.allow + report.step_up + report.block > 0
        entries.append(
            eval_report.FileEntry(
                file=path.name,
                kind="decision",
                pack=pack,
                cases=report.total,
                mismatches=len(report.mismatches),
                allow=report.allow if decided else None,
                step_up=report.step_up if decided else None,
                block=report.block if decided else None,
                false_allow=report.false_allow,
                false_block=report.false_block,
                wrong_reason=report.wrong_reason,
            )
        )

    published = eval_report.build(entries, scenarios)
    print(f"Decision cases: {published.decision_cases}, step-up rate: {published.step_up_rate:.1%}")
    if engine_ms:
        print(f"engine p95: {p95(engine_ms):.3f} ms over {len(engine_ms)} decide() calls")
    failed = published.mismatches > 0
    if args.write_report:
        eval_report.write(published, args.report, args.readme)
        print(f"Wrote {args.report} and the eval table in {args.readme}")
    elif not args.cases:
        stale = eval_report.stale(published, args.report, args.readme)
        for name in stale:
            print(f"Stale: {name}. Run `{eval_report.REGENERATE}` and commit the result.")
        failed |= bool(stale)
    return 1 if failed else 0


def _disputes(path: Path, packs: Path) -> eval_report.FileEntry:
    """Score one Dispute golden file: network codes, Evidence strength, missing evidence."""
    cases = load_dispute_cases(path)
    report = run_dispute_cases(cases, packs)
    pack = f"{cases[0].pack.id}@{cases[0].pack.version}" if cases else "-"
    print(f"{path.name} ({pack})")
    print(f"  cases: {report.total}")
    print(f"  mismatches: {len(report.mismatches)}")
    for m in report.mismatches:
        print(
            f"    {m.case_id} ({m.description}): expected {m.expected.strength} "
            f"{m.expected.visa_code}/{m.expected.mastercard_code} missing "
            f"{list(m.expected.missing)}, got {m.actual.strength} "
            f"{m.actual.visa_code}/{m.actual.mastercard_code} missing {list(m.actual.missing)}"
        )
    return eval_report.FileEntry(
        file=path.name,
        kind="dispute",
        pack=pack,
        cases=report.total,
        mismatches=len(report.mismatches),
    )


def _narratives(path: Path) -> eval_report.FileEntry:
    """Score one citation-checker golden file on labelled model replies."""
    report = run_narrative_cases(load_narrative_cases(path))
    print(f"{path.name} (citation checker)")
    print(f"  cases: {report.total}")
    print(f"  mismatches: {len(report.mismatches)}")
    for m in report.mismatches:
        print(
            f"    {m.case_id} ({m.description}): expected accepted={m.expected.accepted} "
            f"{list(m.expected.codes)}, got accepted={m.actual.accepted} {list(m.actual.codes)}"
        )
    return eval_report.FileEntry(
        file=path.name,
        kind="narrative",
        pack=None,
        cases=report.total,
        mismatches=len(report.mismatches),
    )


if __name__ == "__main__":
    raise SystemExit(main())
