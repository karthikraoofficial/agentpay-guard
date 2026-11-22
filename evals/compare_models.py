"""Model comparison: score each model job's live eval on several Claude models. Calls the
real model, so it costs money and never runs in CI (a test fails if a workflow names it).

    uv run python -m evals.compare_models --spend [--job intent|narrative|all]
        [--models a,b,c] [--passes 3] [--write-report]

Each model runs every case `--passes` times through the ModelGateway port, with the
job's production prompt, prompt version, effort and timeout; only the model changes. The
scoring is each live eval's own (`run_intent_evals.run_cases`, `run_dispute_evals.
run_cases`), taken worst case across passes:

- Intent check: a case counts as correct, and an injection as held, only if every pass
  answered it acceptably. False match, false mismatch and unanswered count the cases
  where it happened on any pass.
- Dispute narrative: a case counts as accepted only if the citation checker accepted it
  (the allowed retry included) on every pass. Rejected counts the cases where, on some
  pass, the checker refused every attempt and the narrative written by code was used;
  unanswered, the cases where a call failed on some pass; injections followed, the
  injection cases that followed one on any pass.

Timeouts and p95 latency are over every call, a timeout counted at the timeout
(`evals.model_calls`). Cost is per full pass, from the gateway's cost on each call.

Before any call it refuses (exit 2) a model the gateway has no price for, then prints a
cost estimate per job: cases x passes x the last known cost per case (from the committed
report, or typical tokens at the model's price before there is one). Without `--spend`,
or without the Anthropic API key, it stops there (exit 2). `--write-report` writes the
committed comparison, evals/model_comparison.json and .md, replacing only the jobs run.
The Eval report's staleness check doesn't cover it: only spending money regenerates it.
"""

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from adapters.claude.gateway import PRICES, ClaudeGateway, cost_usd_micros
from adapters.memory.telemetry import InMemoryTelemetry
from agents.dispute_narrative import EFFORT as NARRATIVE_EFFORT
from agents.dispute_narrative import PROMPT_VERSION as NARRATIVE_PROMPT_VERSION
from core.ports import ModelGateway
from core.settings import Settings, get_settings
from core.signals.intent_check import EFFORT as INTENT_EFFORT
from core.signals.intent_check import PROMPT_VERSION as INTENT_PROMPT_VERSION
from evals import run_dispute_evals, run_intent_evals
from evals.model_calls import TimedGateway
from evals.run_dispute_evals import NarrativeCase
from evals.run_intent_evals import IntentCase

EVALS = Path(__file__).resolve().parent
REPORT_JSON = EVALS / "model_comparison.json"
REPORT_MD = EVALS / "model_comparison.md"
REGENERATE = "uv run python -m evals.compare_models --spend --job all --write-report"
DEFAULT_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")
JOBS = ("intent", "narrative")
# Tokens in and out of one case, for the estimate before a report has the real cost:
# about what the live evals cost on their configured models (Intent: ~$0.003 for 43
# cases on Haiku 5.5; narrative: ~$0.12 for 12 cases on Sonnet 5.5).
TYPICAL_TOKENS = {"intent": (450, 50), "narrative": (3_000, 400)}


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class IntentModelResult(_Strict):
    cases: int
    correct: int  # answered acceptably on every pass
    false_match: int  # cases answered match, where match is not acceptable, on any pass
    false_mismatch: int  # cases answered mismatch, where only match is, on any pass
    injections: int
    injections_held: int  # answered acceptably on every pass
    injection_false_match: int  # injection cases talked into a match on any pass
    unanswered: int  # cases with a failed call (timeout, error, spend cap) on any pass
    timeouts: int  # calls
    cost_usd_micros_per_pass: int
    p95_latency_ms: int | None


class NarrativeModelResult(_Strict):
    cases: int
    accepted: int  # the citation checker accepted it on every pass
    rejected: int  # refused on every attempt, so written by code, on any pass
    unanswered: int  # cases with a failed call (timeout, error, spend cap) on any pass
    injections: int
    injections_followed: int  # on any pass
    timeouts: int  # calls
    cost_usd_micros_per_pass: int
    p95_latency_ms: int | None


class _Header(_Strict):
    date: str
    git_commit: str
    prompt_version: str
    effort: str
    timeout_ms: int
    passes: int


class IntentComparison(_Header):
    models: dict[str, IntentModelResult]


class NarrativeComparison(_Header):
    models: dict[str, NarrativeModelResult]


def compare_intent(
    cases: Sequence[IntentCase],
    gateway: ModelGateway,
    settings: Settings,
    models: Sequence[str],
    passes: int,
) -> dict[str, IntentModelResult]:
    results: dict[str, IntentModelResult] = {}
    for model in models:
        timed = TimedGateway(gateway)
        runs = [run_intent_evals.run_cases(cases, timed, settings, model) for _ in range(passes)]
        correct = false_match = false_mismatch = held = injection_match = unanswered = 0
        for i, case in enumerate(cases):
            answers = [run.results[i] for run in runs]
            right = all(a.answered and a.verdict in case.acceptable for a in answers)
            matched = any(a.verdict == "match" and "match" not in case.acceptable for a in answers)
            correct += right
            false_match += matched
            false_mismatch += case.acceptable == ("match",) and any(
                a.verdict == "mismatch" for a in answers
            )
            unanswered += not all(a.answered for a in answers)
            if case.kind == "injection":
                held += right
                injection_match += matched
        cost = sum(run.cost_usd_micros for run in runs)
        results[model] = IntentModelResult(
            cases=len(cases),
            correct=correct,
            false_match=false_match,
            false_mismatch=false_mismatch,
            injections=sum(c.kind == "injection" for c in cases),
            injections_held=held,
            injection_false_match=injection_match,
            unanswered=unanswered,
            timeouts=timed.timeouts,
            cost_usd_micros_per_pass=-(-cost // passes),
            p95_latency_ms=timed.p95_latency_ms,
        )
    return results


def compare_narrative(
    cases: Sequence[NarrativeCase],
    gateway: ModelGateway,
    settings: Settings,
    models: Sequence[str],
    passes: int,
) -> dict[str, NarrativeModelResult]:
    results: dict[str, NarrativeModelResult] = {}
    for model in models:
        timed = TimedGateway(gateway)
        runs = [run_dispute_evals.run_cases(cases, timed, settings, model) for _ in range(passes)]
        accepted = rejected = unanswered = followed = 0
        for i, case in enumerate(cases):
            outcomes = [run.outcomes[i] for run in runs]
            accepted += all(o.accepted for o in outcomes)
            rejected += any(not o.accepted and not o.call_failed for o in outcomes)
            unanswered += any(o.call_failed for o in outcomes)
            followed += case.kind == "injection" and any(o.followed for o in outcomes)
        cost = sum(run.cost_usd_micros for run in runs)
        results[model] = NarrativeModelResult(
            cases=len(cases),
            accepted=accepted,
            rejected=rejected,
            unanswered=unanswered,
            injections=sum(c.kind == "injection" for c in cases),
            injections_followed=followed,
            timeouts=timed.timeouts,
            cost_usd_micros_per_pass=-(-cost // passes),
            p95_latency_ms=timed.p95_latency_ms,
        )
    return results


def _usd(micros: int) -> str:
    return f"${micros // 1_000_000}.{micros % 1_000_000:06d}"


def _p95(ms: int | None) -> str:
    return "n/a" if ms is None else f"{ms} ms"


def _run_line(job: _Header) -> str:
    return (
        f"Run {job.date} at `{job.git_commit}`: prompt `{job.prompt_version}`, effort "
        f"{job.effort}, timeout {job.timeout_ms} ms, {job.passes} passes per model."
    )


def _intent_section(job: IntentComparison) -> list[str]:
    lines = [
        "## Intent check",
        "",
        _run_line(job),
        "",
        "| model | correct | false match | false mismatch | injections held | unanswered "
        "| timeouts | cost per pass | p95 latency |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for model, r in job.models.items():
        lines.append(
            f"| {model} | {r.correct}/{r.cases} | {r.false_match} | {r.false_mismatch} "
            f"| {r.injections_held}/{r.injections} | {r.unanswered} | {r.timeouts} "
            f"| {_usd(r.cost_usd_micros_per_pass)} | {_p95(r.p95_latency_ms)} |"
        )
    return lines


def _narrative_section(job: NarrativeComparison) -> list[str]:
    lines = [
        "## Dispute narrative",
        "",
        _run_line(job),
        "",
        "| model | accepted | rejected, written by code | unanswered | timeouts "
        "| injections followed | cost per pass | p95 latency |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for model, r in job.models.items():
        lines.append(
            f"| {model} | {r.accepted}/{r.cases} | {r.rejected} | {r.unanswered} "
            f"| {r.timeouts} | {r.injections_followed}/{r.injections} "
            f"| {_usd(r.cost_usd_micros_per_pass)} | {_p95(r.p95_latency_ms)} |"
        )
    return lines


def render(jobs: dict[str, Any]) -> str:
    lines = [
        "# Model comparison",
        "",
        f"Generated by `{REGENERATE}`, by hand: it calls real models and costs money, so it "
        "never runs in CI. Every case runs once per pass on each model, with the job's "
        "production prompt, effort and timeout. Each case is scored by its worst pass: a "
        "case counts as correct, accepted or held only if every pass got it right, and as "
        "a false match, rejected, unanswered or followed if any pass did. Timeouts and p95 "
        "latency are over every call, a timeout counted at the timeout. Raw numbers: "
        "[model_comparison.json](model_comparison.json).",
    ]
    if "intent" in jobs:
        lines += ["", *_intent_section(IntentComparison.model_validate(jobs["intent"]))]
    if "narrative" in jobs:
        lines += ["", *_narrative_section(NarrativeComparison.model_validate(jobs["narrative"]))]
    return "\n".join(lines) + "\n"


def _load_jobs(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    jobs: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")).get("jobs", {})
    return jobs


def _cost_per_case(model: str, job: str, jobs: dict[str, Any]) -> int:
    known = jobs.get(job, {}).get("models", {}).get(model)
    if known and known["cases"]:
        return -(-int(known["cost_usd_micros_per_pass"]) // int(known["cases"]))
    return cost_usd_micros(model, *TYPICAL_TOKENS[job])


def _git_commit() -> str:
    command = ["git", "rev-parse", "--short", "HEAD"]
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()  # noqa: S603


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _run_job(
    job: str,
    cases: Sequence[Any],
    gateway: ModelGateway,
    settings: Settings,
    models: Sequence[str],
    passes: int,
) -> _Header:
    header: dict[str, Any] = {"date": _today(), "git_commit": _git_commit(), "passes": passes}
    if job == "intent":
        return IntentComparison(
            **header,
            prompt_version=INTENT_PROMPT_VERSION,
            effort=INTENT_EFFORT,
            timeout_ms=settings.intent_check_timeout_ms,
            models=compare_intent(cases, gateway, settings, models, passes),
        )
    return NarrativeComparison(
        **header,
        prompt_version=NARRATIVE_PROMPT_VERSION,
        effort=NARRATIVE_EFFORT,
        timeout_ms=settings.dispute_narrative_timeout_ms,
        models=compare_narrative(cases, gateway, settings, models, passes),
    )


def main(argv: list[str] | None = None, gateway: ModelGateway | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.compare_models")
    parser.add_argument("--job", choices=(*JOBS, "all"), default="intent")
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS), help="comma-separated")
    parser.add_argument("--passes", type=int, default=3)
    parser.add_argument("--spend", action="store_true", help="call the real models (costs money)")
    parser.add_argument("--write-report", action="store_true", help="update the committed report")
    parser.add_argument("--cases", type=Path, default=run_intent_evals.CASES)
    parser.add_argument("--narrative-cases", type=Path, default=run_dispute_evals.CASES)
    parser.add_argument("--report-json", type=Path, default=REPORT_JSON, help=argparse.SUPPRESS)
    parser.add_argument("--report-md", type=Path, default=REPORT_MD, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.passes < 1:
        parser.error("--passes must be at least 1")
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    unpriced = [m for m in models if m not in PRICES]
    if unpriced or not models:
        print(
            f"No price for {', '.join(unpriced) or 'any model'}; the gateway prices "
            f"{', '.join(PRICES)}. Add a model to PRICES before comparing it.",
            file=sys.stderr,
        )
        return 2

    to_run = JOBS if args.job == "all" else (args.job,)
    cases: dict[str, Sequence[Any]] = {}
    for job in to_run:
        if job == "intent":
            cases[job] = run_intent_evals.load_cases(args.cases)
        else:
            cases[job] = run_dispute_evals.load_cases(args.narrative_cases)
    jobs = _load_jobs(args.report_json)
    total = 0
    for job in to_run:
        n = len(cases[job])
        per_model = {m: n * args.passes * _cost_per_case(m, job, jobs) for m in models}
        total += sum(per_model.values())
        print(
            f"estimate: {n} cases x {args.passes} passes on {len(models)} models ({job}), "
            f"about {_usd(sum(per_model.values()))} ("
            + ", ".join(f"{m} {_usd(c)}" for m, c in per_model.items())
            + ")"
        )
    if len(to_run) > 1:
        print(f"estimate in all: about {_usd(total)}")
    if not args.spend:
        print("Not run: pass --spend to call the real models and pay for it.", file=sys.stderr)
        return 2

    settings = get_settings()
    if gateway is None:
        if settings.anthropic_api_key is None:
            print("Set AGENTPAY_ANTHROPIC_API_KEY to run the comparison.", file=sys.stderr)
            return 2
        gateway = ClaudeGateway(settings.anthropic_api_key, InMemoryTelemetry())

    for job in to_run:
        result = _run_job(job, cases[job], gateway, settings, models, args.passes)
        jobs[job] = result.model_dump(mode="json")
    text = render(jobs)
    print(text)
    if args.write_report:
        report = {"about": f"Generated by `{REGENERATE}`. Do not edit by hand.", "jobs": jobs}
        args.report_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        args.report_md.write_text(text, encoding="utf-8")
        print(f"wrote {args.report_json.name} and {args.report_md.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
