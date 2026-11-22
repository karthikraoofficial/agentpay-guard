"""The Demo runner's browser API.

- `POST /runs` `{"scenario": "A1"}`: start a run (201). The answer holds an unguessable
  run id and the number of steps. Each run grants its own Payer, Agent and Mandate.
- `POST /runs/{run_id}/next` `{"step": n, "press": false}`: run step n and return its Card.
  A fast-forward step waits until `press` is true (ADR-0032). Asking again
  for the step just run returns its Card without running it twice; a step that failed
  runs again with the same idempotency key. A waiting Card means: ask for the same step
  again later.
- `GET /healthz`.

Rate limits: runs started per client IP per minute, and runs in progress overall. Past
either the answer is 429 with Retry-After. Runs live in this process for their TTL, so a
restart ends them (one runner on one VM, ADR-0021).

Keys never leave the two HTTP clients' headers: not into a Card, a log line or an error.
"""

import asyncio
import logging
import math
import secrets
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.settings import DemoRunnerSettings
from demo.engine import Card, Run, StepFailed, WrongStep
from demo.scenario import Scenario, catalogue

log = logging.getLogger(__name__)

_WINDOW_SECONDS = 60
# What a busy runner tells the browser: runs end within minutes as visitors finish.
_BUSY_RETRY_SECONDS = 30


class StartBody(BaseModel):
    scenario: str = Field(min_length=1, max_length=8)


class NextBody(BaseModel):
    step: int | None = Field(default=None, ge=0)
    # The visitor pressed the fast-forward this step is waiting on (ADR-0032).
    press: bool = False


class Started(BaseModel):
    run_id: str
    scenario: str
    steps: int


@dataclass
class _Live:
    run: Run
    started: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _problem(status: int, code: str, detail: str, retry_after: int | None = None) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return JSONResponse({"code": code, "detail": detail}, status_code=status, headers=headers)


def create_app(
    settings: DemoRunnerSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    seconds: Callable[[], float] = time.monotonic,
    scenarios: dict[str, Scenario] | None = None,
) -> FastAPI:
    """`transport`, `now` and `seconds` are for tests: the API in-process, the API's clock,
    and the monotonic clock the rate limits and TTL read."""
    known = catalogue() if scenarios is None else scenarios
    base_url = str(settings.demo_runner_api_base_url)
    timeout = settings.demo_runner_http_timeout_seconds

    def client(key: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
            transport=transport,
        )

    agent = client(settings.demo_agent_key.get_secret_value())
    tenant = client(settings.demo_tenant_key.get_secret_value())
    runs: dict[str, _Live] = {}
    starts: dict[str, deque[float]] = {}
    ttl = settings.demo_runner_run_ttl_seconds

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await agent.aclose()
        await tenant.aclose()

    app = FastAPI(title="AgentPay Guard Demo runner", lifespan=lifespan, docs_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.web_origin],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    def forget_old(at: float) -> None:
        for run_id in [r for r, live in runs.items() if at - live.started >= ttl]:
            del runs[run_id]
        for ip in list(starts):
            window = starts[ip]
            while window and at - window[0] >= _WINDOW_SECONDS:
                window.popleft()
            if not window:
                del starts[ip]

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/runs", status_code=201, response_model=Started)
    async def start(body: StartBody, request: Request) -> Started | JSONResponse:
        scenario = known.get(body.scenario)
        if scenario is None:
            return _problem(404, "SCENARIO_NOT_FOUND", f"No Scenario {body.scenario}.")
        at = seconds()
        forget_old(at)
        ip = request.client.host if request.client else "unknown"
        window = starts.setdefault(ip, deque())
        if len(window) >= settings.demo_runner_runs_per_ip_per_minute:
            wait = math.ceil(_WINDOW_SECONDS - (at - window[0]))
            return _problem(429, "TOO_MANY_RUNS", "Too many demo runs from here.", wait)
        in_progress = sum(1 for live in runs.values() if not live.run.finished)
        if in_progress >= settings.demo_runner_runs_in_progress_max:
            return _problem(429, "RUNNER_BUSY", "The demo is busy.", _BUSY_RETRY_SECONDS)
        window.append(at)
        run_id = secrets.token_urlsafe(24)
        run = Run(scenario, agent=agent, tenant=tenant, now=now)
        runs[run_id] = _Live(run, at)
        log.info("run=%s scenario=%s started", run_id[:8], scenario.id)
        return Started(run_id=run_id, scenario=scenario.id, steps=run.total)

    @app.post("/runs/{run_id}/next", response_model=Card)
    async def next_step(run_id: str, body: NextBody) -> Card | JSONResponse:
        forget_old(seconds())
        live = runs.get(run_id)
        if live is None:
            return _problem(404, "RUN_NOT_FOUND", "No such run: it may have expired.")
        async with live.lock:
            try:
                card = await live.run.next(body.step, press=body.press)
            except WrongStep as wrong:
                return _problem(409, "WRONG_STEP", str(wrong))
            except StepFailed as failed:
                log.info("run=%s step=%s failed: %s", run_id[:8], failed.step, failed.detail)
                return _problem(502, "STEP_FAILED", f"{failed.step}: {failed.detail}")
        log.info(
            "run=%s step=%s call=%s outcome=%s waiting=%s",
            run_id[:8],
            card.step,
            card.call.name if card.call else "-",
            card.decision.outcome if card.decision else "-",
            card.waiting,
        )
        return card

    return app
