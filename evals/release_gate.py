"""The Release gate: may a Candidate pack become the active Policy pack?

Every Decision and approval golden case is decided twice, on the active pack and on the
candidate, whichever pack its file is pinned to, and the two answers are compared. The
labels play no part: changes earlier releases made on purpose are already in the active
pack's answers, so only what the candidate changes shows up (ADR-0027).

A change passes only when the candidate declares it, in a declarations file beside its
pack file (`<id>/<version>.declared-changes.yaml`), with both answers and a reason. The
file isn't part of the pack, so it changes no Pack hash (ADR-0028).
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from core.policy.packs import PackError, load_active_pack, load_pack
from evals.run_evals import (
    GOLDEN,
    PACKS,
    Answer,
    Expected,
    PackPin,
    decide_case,
    is_decision_file,
    load_cases,
    run_cases,
)

# Looser is toward letting the payment through. A Consent resolution other than approved
# keeps the payment from going through, so all of them rank above approved.
_STRICTNESS: dict[str, int] = {"allow": 0, "step_up": 1, "block": 2}
_RESOLUTION_STRICTNESS: dict[str, int] = {"approved": 0, "declined": 1, "expired": 1, "blocked": 1}


class DeclaredChange(BaseModel):
    """A golden case whose answer the candidate is meant to change, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str = Field(description="The golden file's name, for example phase2.jsonl")
    case_id: str
    active: Answer = Field(description="The case's answer on the active pack")
    candidate: Answer = Field(description="The case's answer on the candidate")
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

    @model_validator(mode="after")
    def _is_a_change(self) -> Self:
        if self.active == self.candidate:
            raise ValueError(f"{self.case_id}: the same answer on both packs is not a change")
        return self


class Declarations(BaseModel):
    """A Candidate pack's declarations file: every change it makes on purpose."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    changes: list[DeclaredChange] = Field(min_length=1)

    @model_validator(mode="after")
    def _each_case_once(self) -> Self:
        seen: set[tuple[str, str]] = set()
        for d in self.changes:
            if (d.file, d.case_id) in seen:
                raise ValueError(f"{d.file} {d.case_id} is declared more than once")
            seen.add((d.file, d.case_id))
        return self


class DeclarationsError(Exception):
    """A declarations file can't be read or fails its schema."""


def declarations_path(pack_root: Path, pin: PackPin) -> Path:
    return pack_root / pin.id / f"{pin.version}.declared-changes.yaml"


def load_declarations(pack_root: Path, pin: PackPin) -> list[DeclaredChange]:
    """The candidate's Declared changes; none when it has no declarations file."""
    path = declarations_path(pack_root, pin)
    if not path.exists():
        return []
    try:
        return Declarations.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))).changes
    except (OSError, yaml.YAMLError, ValueError) as e:  # pydantic's ValidationError included
        raise DeclarationsError(f"{path} could not be loaded: {e}") from e


def write_schema(pack_root: Path) -> Path:
    """Write the JSON Schema for declarations files, generated from the model, beside the
    pack schema."""
    path = pack_root / "declared-changes.schema.json"
    path.write_text(
        json.dumps(Declarations.model_json_schema(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


class Change(BaseModel):
    """A golden case whose answer the candidate changes."""

    file: str
    case_id: str
    active: Answer
    candidate: Answer
    # True when a Declared change names this case with these exact answers.
    declared: bool = False

    @property
    def loosens(self) -> bool:
        if self.active.resolution is not None and self.candidate.resolution is not None:
            was = _RESOLUTION_STRICTNESS[self.active.resolution]
            now = _RESOLUTION_STRICTNESS[self.candidate.resolution]
        else:
            was, now = _STRICTNESS[self.active.outcome], _STRICTNESS[self.candidate.outcome]
        return now < was

    @property
    def high_risk(self) -> bool:
        return "high" in (self.active.risk_tier, self.candidate.risk_tier)


class LabelMismatch(BaseModel):
    """A case in a golden file pinned to the candidate whose label the candidate misses."""

    file: str
    case_id: str
    expected: Expected
    actual: Expected


class WrongDeclaration(BaseModel):
    """A Declared change that doesn't match what the case does: it doesn't change, or
    changes to a different answer. Holds what the case actually returned on each pack,
    None when no Decision or approval golden case has that id."""

    declared: DeclaredChange
    active: Answer | None
    candidate: Answer | None


class StepUpRate(BaseModel):
    """Share of decision-stage cases that step up, on each pack. Approval cases aren't
    counted, as in the Eval report."""

    active: float
    candidate: float


class Verdict(BaseModel):
    compared: int
    changes: list[Change]
    step_up_rate: StepUpRate
    wrong_declarations: list[WrongDeclaration] = []
    own_file_mismatches: list[LabelMismatch] = []
    # Cases that couldn't be decided, as "<file> <case id>: <why>". Never skipped silently.
    errors: list[str] = []

    @property
    def undeclared(self) -> list[Change]:
        return [c for c in self.changes if not c.declared]

    @property
    def high_risk_loosenings(self) -> list[Change]:
        """Changes that fail the gate even when declared (#234)."""
        return [c for c in self.changes if c.loosens and c.high_risk]

    @property
    def passed(self) -> bool:
        return not (
            self.undeclared
            or self.wrong_declarations
            or self.high_risk_loosenings
            or self.own_file_mismatches
            or self.errors
        )


def run_gate(
    pack_root: Path,
    golden: Path,
    active: PackPin,
    candidate: PackPin,
    declared: Sequence[DeclaredChange] = (),
) -> Verdict:
    """Decide every Decision and approval golden case on both packs. The verdict passes
    when the changes are exactly the `declared` ones, none is a high-risk loosening, and
    the candidate meets the labels in its own golden files."""
    before = load_pack(pack_root, active.id, active.version).pack
    after = load_pack(pack_root, candidate.id, candidate.version).pack
    compared = 0
    changes: list[Change] = []
    own: list[LabelMismatch] = []
    errors: list[str] = []
    answers: dict[tuple[str, str], tuple[Answer, Answer]] = {}
    decisions = stepped_up_before = stepped_up_after = 0
    for path in sorted(golden.glob("*.jsonl")):
        if not is_decision_file(path):
            continue
        decided = []
        for case in load_cases(path):
            compared += 1
            try:
                was, _ = decide_case(case, before)
                now, _ = decide_case(case, after)
            except ValueError as e:  # pydantic's ValidationError included
                errors.append(f"{path.name} {case.id}: {e}")
                continue
            decided.append(case)
            answers[(path.name, case.id)] = (was, now)
            if case.stage == "decision":
                decisions += 1
                stepped_up_before += was.outcome == "step_up"
                stepped_up_after += now.outcome == "step_up"
            if was != now:
                changes.append(Change(file=path.name, case_id=case.id, active=was, candidate=now))
        # The candidate's own golden files must still pass the eval thresholds.
        pinned = [c for c in decided if c.pack == candidate]
        for m in run_cases(pinned, pack_root).mismatches:
            own.append(
                LabelMismatch(
                    file=path.name, case_id=m.case_id, expected=m.expected, actual=m.actual
                )
            )
    wrong: list[WrongDeclaration] = []
    for d in declared:
        was_now = answers.get((d.file, d.case_id))
        if was_now == (d.active, d.candidate):
            continue
        actual = was_now or (None, None)
        wrong.append(WrongDeclaration(declared=d, active=actual[0], candidate=actual[1]))
    matched = {(d.file, d.case_id) for d in declared} - {
        (w.declared.file, w.declared.case_id) for w in wrong
    }
    changes = [c.model_copy(update={"declared": (c.file, c.case_id) in matched}) for c in changes]
    rate = StepUpRate(
        active=stepped_up_before / decisions if decisions else 0.0,
        candidate=stepped_up_after / decisions if decisions else 0.0,
    )
    return Verdict(
        compared=compared,
        changes=changes,
        step_up_rate=rate,
        wrong_declarations=wrong,
        own_file_mismatches=own,
        errors=errors,
    )


def _pin(text: str) -> PackPin:
    pack_id, _, version = text.partition("@")
    if not pack_id or not version:
        raise PackError(f"{text!r} is not a pack pin; write it as <id>@<version>")
    return PackPin(id=pack_id, version=version)


def _answer(a: Answer) -> str:
    resolution = f" {a.resolution}" if a.resolution else ""
    return f"{a.outcome} {a.risk_tier} {list(a.reason_codes)}{resolution}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.release_gate",
        description="Exit 0 if the Candidate pack passes the Release gate, 1 on a "
        "regression, 2 on a usage or input error.",
    )
    parser.add_argument("--active", help="<id>@<version> (default: the Manifest's active)")
    parser.add_argument("--candidate", help="<id>@<version> (default: the Manifest's active)")
    parser.add_argument("--packs", type=Path, default=PACKS)
    parser.add_argument("--golden", type=Path, default=GOLDEN, help=argparse.SUPPRESS)
    parser.add_argument(
        "--write-schema",
        action="store_true",
        help="write <packs>/declared-changes.schema.json and exit",
    )
    args = parser.parse_args(argv)
    if args.write_schema:
        print(write_schema(args.packs))
        return 0
    try:
        default = f"default@{load_active_pack(args.packs, 'default').pack.version}"
        active = _pin(args.active or default)
        candidate = _pin(args.candidate or default)
        declared = load_declarations(args.packs, candidate)
        verdict = run_gate(args.packs, args.golden, active, candidate, declared)
    except (PackError, DeclarationsError) as e:
        print(f"release gate: {e}", file=sys.stderr)
        return 2

    print(f"Release gate: {active.id}@{active.version} -> {candidate.id}@{candidate.version}")
    for c in verdict.changes:
        mark = "[declared]" if c.declared else "[undeclared]"
        print(f"  {mark} {c.file} {c.case_id}: {_answer(c.active)} -> {_answer(c.candidate)}")
    for w in verdict.wrong_declarations:
        d = w.declared
        got = (
            f"{_answer(w.active)} -> {_answer(w.candidate)}"
            if w.active and w.candidate
            else "no Decision or approval golden case with that id"
        )
        print(
            f"  wrong declaration: {d.file} {d.case_id}: declared "
            f"{_answer(d.active)} -> {_answer(d.candidate)}, got {got}"
        )
    for c in verdict.high_risk_loosenings:
        print(f"  high-risk loosening: {c.file} {c.case_id}")
    for m in verdict.own_file_mismatches:
        print(
            f"  label missed: {m.file} {m.case_id}: expected {m.expected.outcome} "
            f"{list(m.expected.reason_codes)}, got {m.actual.outcome} {list(m.actual.reason_codes)}"
        )
    for error in verdict.errors:
        print(f"  could not decide: {error}")
    rate = verdict.step_up_rate
    print(f"compared: {verdict.compared}")
    print(f"changed: {len(verdict.changes)}")
    print(f"declared: {len(verdict.changes) - len(verdict.undeclared)}")
    print(f"undeclared: {len(verdict.undeclared)}")
    print(f"wrong declarations: {len(verdict.wrong_declarations)}")
    print(f"high-risk loosenings: {len(verdict.high_risk_loosenings)}")
    print(f"step-up rate: {rate.active:.1%} -> {rate.candidate:.1%}")
    print("PASS" if verdict.passed else "FAIL")
    return 0 if verdict.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
