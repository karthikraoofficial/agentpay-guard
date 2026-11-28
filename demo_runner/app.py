"""The Demo runner's browser API.

- `POST /runs` `{"scenario": "A1"}`: start a run (201). The answer holds an unguessable
  run id and the number of steps. Each run grants its own Payer, Agent and Mandate.
- `POST /runs/{run_id}/next` `{"step": n, "press": false}`: run step n and return its Card.
  A fast-forward step waits until `press` is true (ADR-0032). Asking again
  for the step just run returns its Card without running it twice; a step that failed
  runs again with the same idempotency key. A waiting Card means: ask for the same step
  again later.
- `POST /runs/{run_id}/verify`: the inspector's Verify / Replay button (#275). Replays this
  run's Decision and walks the Demo Tenant's Audit chain, at most 10 times a run.
- `POST /webhooks`: the Demo Tenant's Tenant webhook endpoint (#286). A delivery whose
  signature checks out against `AGENTPAY_DEMO_RUNNER_WEBHOOK_SECRET` is kept for the run
  it is about (200); any other is refused (400 `BAD_SIGNATURE`). Without the secret the
  route doesn't exist, and a Scenario that shows webhooks (H8) can't start (409
  `WEBHOOKS_OFF`).
- `GET /status`: whether a simulated rail outage is running, for the banner on /demo
  (ADR-0032). Read from the API at most every 5 seconds.
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
from demo.engine import Card, Run, StepFailed, Verification, WrongStep, needs_webhooks
from demo.scenario import Scenario, catalogue
from demo.webhook_inbox import BadSignature, WebhookInbox

log = logging.getLogger(__name__)

_WINDOW_SECONDS = 60
# What a busy runner tells the browser: runs end within minutes as visitors finish.
_BUSY_RETRY_SECONDS = 30
# Verify walks the Demo Tenant's whole Audit chain, so a public page can't ask without end.
_VERIFIES_PER_RUN = 10
# Every open /demo page polls the status, so the API is asked at most this often.
_STATUS_CACHE_SECONDS = 5.0
# A Tenant webhook body holds ids, statuses and times only: anything bigger isn't one.
_WEBHOOK_MAX_BYTES = 64 * 1024


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


class DemoStatus(BaseModel):
    """For the banner on /demo: whether a simulated rail outage is running (ADR-0032)."""

    rail_outage: bool = False
    outage_ends_at: datetime | None = None


@dataclass
class _Live:
    run: Run
    started: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    verifies: int = 0


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
    status_read: tuple[float, DemoStatus] | None = None
    secret = settings.demo_runner_webhook_secret
    inbox = None if secret is None else WebhookInbox(secret.get_secret_value(), now=now)

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

    @app.get("/status", response_model=DemoStatus)
    async def status() -> DemoStatus:
        """No answer from the API means no banner: the page itself still works."""
        nonlocal status_read
        at = seconds()
        if status_read is not None and at - status_read[0] < _STATUS_CACHE_SECONDS:
            return status_read[1]
        current = DemoStatus()
        try:
            response = await tenant.get("/v1/demo/status")
            if response.is_success:
                current = DemoStatus.model_validate(response.json())
        except (httpx.HTTPError, ValueError):
            log.info("the API's demo status couldn't be read")
        status_read = (at, current)
        return current

    @app.post("/runs", status_code=201, response_model=Started)
    async def start(body: StartBody, request: Request) -> Started | JSONResponse:
        scenario = known.get(body.scenario)
        if scenario is None:
            return _problem(404, "SCENARIO_NOT_FOUND", f"No Scenario {body.scenario}.")
        if needs_webhooks(scenario) and inbox is None:
            return _problem(409, "WEBHOOKS_OFF", "This demo has no Tenant webhook receiver set up.")
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
        run = Run(scenario, agent=agent, tenant=tenant, now=now, inbox=inbox)
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

    if inbox is not None:

        @app.post("/webhooks")
        async def webhooks(request: Request) -> JSONResponse:
            """The API's worker posts here, not a browser: CORS doesn't apply. Only the
            event's id and type are logged, never its body or signature."""
            raw = await request.body()
            if len(raw) > _WEBHOOK_MAX_BYTES:
                return _problem(413, "TOO_LARGE", "That isn't a Tenant webhook.")
            try:
                event = inbox.receive(raw, request.headers)
            except BadSignature:
                log.info("a webhook delivery was refused: its signature didn't verify")
                return _problem(400, "BAD_SIGNATURE", "The signature didn't verify.")
            log.info("webhook %s %s received", event.event_type, event.event_id)
            return JSONResponse({"received": event.event_id})

    @app.post("/runs/{run_id}/verify", response_model=Verification)
    async def verify(run_id: str) -> Verification | JSONResponse:
        at = seconds()
        forget_old(at)
        live = runs.get(run_id)
        if live is None:
            return _problem(404, "RUN_NOT_FOUND", "No such run: it may have expired.")
        async with live.lock:
            if live.run.decision is None:
                return _problem(409, "NO_DECISION_YET", "This run has no Decision to check yet.")
            if live.verifies >= _VERIFIES_PER_RUN:
                wait = max(1, math.ceil(ttl - (at - live.started)))
                return _problem(429, "TOO_MANY_VERIFIES", "This run has checked enough.", wait)
            live.verifies += 1
            try:
                checked = await live.run.verify()
            except StepFailed as failed:
                log.info("run=%s verify failed: %s", run_id[:8], failed.detail)
                return _problem(502, "STEP_FAILED", f"{failed.step}: {failed.detail}")
        if checked is None:  # pragma: no cover - the run's Decision was checked above
            return _problem(409, "NO_DECISION_YET", "This run has no Decision to check yet.")
        return checked

    return app
