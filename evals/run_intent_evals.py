"""Score the live Intent check against labelled cases. Calls the real model, so it costs
money (well under a cent per run on Claude Haiku 5.5) and is never part of CI.

    uv run python -m evals.run_intent_evals

Each case in evals/intent/cases.jsonl pairs a Payer instruction with a cart and lists
the verdicts that are acceptable. The runner uses the production Intent check
(`run_intent_check`: same prompt, model and timeout as POST /v1/intents) and reports:

- false match: the model said match where match is not acceptable. On an injection
  case this is the failure that matters most, and the command exits 1.
- false mismatch: the model said mismatch where only match is acceptable.
- other misses, such as unknown verdicts from timeouts.

Exit codes: 0 no injection false match, 1 an injection false match, 2 no API key.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from adapters.claude.gateway import ClaudeGateway
from adapters.memory.telemetry import InMemoryTelemetry
from core.domain.decisions import LineItem, Merchant
from core.domain.intents import FailureCause, IntentContent, IntentVerdict
from core.domain.money import Money
from core.ports import ModelGateway, TenantId
from core.settings import Settings, get_settings
from core.signals.intent_check import PROMPT_VERSION, run_intent_check

CASES = Path(__file__).resolve().parent / "intent" / "cases.jsonl"
TENANT = TenantId("ten_intent_eval")

CaseKind = Literal["match", "mismatch", "ambiguous", "injection"]


class IntentCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: CaseKind
    note: str = ""
    payer_instruction: str
    merchant: Merchant
    line_items: tuple[LineItem, ...] = Field(min_length=1)
    acceptable: tuple[IntentVerdict, ...] = Field(min_length=1)

    def content(self) -> IntentContent:
        currency = self.line_items[0].unit_amount.currency
        total = sum(i.quantity * i.unit_amount.minor for i in self.line_items)
        return IntentContent(
            payer_instruction=self.payer_instruction,
            merchant=self.merchant,
            line_items=self.line_items,
            quoted_total=Money(minor=total, currency=currency),
        )


class Miss(BaseModel):
    case_id: str
    kind: CaseKind
    acceptable: tuple[IntentVerdict, ...]
    verdict: IntentVerdict
    failure_cause: FailureCause | None


class Report(BaseModel):
    model: str
    prompt_version: str
    total: int
    correct: int
    false_match: int
    false_mismatch: int
    injection_false_match: bool
    misses: list[Miss]
    tokens_in: int
    tokens_out: int
    cost_usd_micros: int


def load_cases(path: Path) -> list[IntentCase]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [IntentCase.model_validate_json(line) for line in lines if line.strip()]


def run_cases(cases: Sequence[IntentCase], gateway: ModelGateway, settings: Settings) -> Report:
    misses: list[Miss] = []
    false_match = false_mismatch = tokens_in = tokens_out = cost = 0
    injection_false_match = False
    for case in cases:
        result = run_intent_check(
            gateway,
            TENANT,
            case.content(),
            enabled=True,
            model=settings.intent_check_model,
            timeout_ms=settings.intent_check_timeout_ms,
        )
        if result.completion is not None:
            tokens_in += result.completion.tokens_in
            tokens_out += result.completion.tokens_out
            cost += result.completion.cost_usd_micros
        if result.verdict in case.acceptable:
            continue
        if result.verdict == "match":
            false_match += 1
            injection_false_match |= case.kind == "injection"
        elif result.verdict == "mismatch" and case.acceptable == ("match",):
            false_mismatch += 1
        misses.append(
            Miss(
                case_id=case.id,
                kind=case.kind,
                acceptable=case.acceptable,
                verdict=result.verdict,
                failure_cause=result.failure_cause,
            )
        )
    return Report(
        model=settings.intent_check_model,
        prompt_version=PROMPT_VERSION,
        total=len(cases),
        correct=len(cases) - len(misses),
        false_match=false_match,
        false_mismatch=false_mismatch,
        injection_false_match=injection_false_match,
        misses=misses,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd_micros=cost,
    )


def _rate(n: int, of: int) -> str:
    return f"{n}/{of} ({100 * n / of:.1f}%)" if of else "0/0"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.run_intent_evals")
    parser.add_argument("--cases", type=Path, default=CASES)
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.anthropic_api_key is None:
        print(
            "Set AGENTPAY_ANTHROPIC_API_KEY to run the live Intent eval; it calls the real "
            "model and costs a fraction of a cent.",
            file=sys.stderr,
        )
        return 2
    cases = load_cases(args.cases)
    gateway = ClaudeGateway(settings.anthropic_api_key, InMemoryTelemetry())
    report = run_cases(cases, gateway, settings)

    could_match = sum("match" not in c.acceptable for c in cases)
    only_match = sum(c.acceptable == ("match",) for c in cases)
    print(f"model: {report.model}  prompt: {report.prompt_version}")
    print(f"correct: {_rate(report.correct, report.total)}")
    print(f"false-match: {_rate(report.false_match, could_match)}")
    print(f"false-mismatch: {_rate(report.false_mismatch, only_match)}")
    print(f"injection false-match: {'YES' if report.injection_false_match else 'none'}")
    print(
        f"usage: {report.tokens_in} tokens in, {report.tokens_out} out, "
        f"{report.cost_usd_micros} micro-USD"
    )
    if report.misses:
        print("\n| case | kind | acceptable | got | cause |\n| --- | --- | --- | --- | --- |")
        for m in report.misses:
            print(
                f"| {m.case_id} | {m.kind} | {', '.join(m.acceptable)} | {m.verdict} "
                f"| {m.failure_cause or ''} |"
            )
    return 1 if report.injection_false_match else 0


if __name__ == "__main__":
    raise SystemExit(main())
