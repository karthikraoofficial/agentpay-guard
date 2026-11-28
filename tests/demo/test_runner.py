"""The Demo runner's browser API (#273): start a run for a Scenario, then `next` runs one
step and returns its Card. The runner holds the Demo Tenant's keys and sends to the
in-process app; the browser never sees a key."""

import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from core.domain.webhooks import EVENT_ID_HEADER, SIGNATURE_HEADER, body, signature
from core.settings import DemoRunnerSettings
from demo_runner.__main__ import main
from demo_runner.app import create_app
from tests.api.conftest import Api
from tests.demo.conftest import BASE_URL, transport_to
from tests.demo.operator import RECEIVER, Operator, register_receiver

pytestmark = pytest.mark.anyio

WEB = "https://demo.example"


def runner_settings(keys: tuple[str, str], **overrides: Any) -> DemoRunnerSettings:
    return DemoRunnerSettings(
        _env_file=None,  # type: ignore[call-arg]
        demo_agent_key=SecretStr(keys[0]),
        demo_tenant_key=SecretStr(keys[1]),
        demo_runner_api_base_url=BASE_URL,
        web_origin=WEB,
        **overrides,
    )


class Seconds:
    """A monotonic clock the test moves."""

    def __init__(self) -> None:
        self.at = 1000.0

    def __call__(self) -> float:
        return self.at


@asynccontextmanager
async def runner(
    api: Api,
    keys: tuple[str, str],
    *,
    ip: str = "203.0.113.7",
    seconds: Callable[[], float] | None = None,
    api_transport: httpx.AsyncBaseTransport | None = None,
    **overrides: Any,
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        runner_settings(keys, **overrides),
        transport=api_transport or transport_to(api),
        now=api.clock.now,
        seconds=seconds or Seconds(),
    )
    browser = httpx.ASGITransport(app=app, client=(ip, 50000))
    async with httpx.AsyncClient(transport=browser, base_url="http://runner.test") as client:
        yield client


async def start(client: httpx.AsyncClient, scenario: str = "A1") -> httpx.Response:
    return await client.post("/runs", json={"scenario": scenario})


async def play_through(client: httpx.AsyncClient, run_id: str, operator: Operator) -> list[Any]:
    cards: list[Any] = []
    step = 0
    for _ in range(40):
        response = await client.post(f"/runs/{run_id}/next", json={"step": step})
        assert response.status_code == 200, response.text
        card = response.json()
        if card["waiting"] and card["fast_forward"]:  # the visitor presses it, as on /demo
            pressed = await client.post(f"/runs/{run_id}/next", json={"step": step, "press": True})
            assert pressed.status_code == 200, pressed.text
            card = pressed.json()
        cards.append(card)
        if card["finished"]:
            return cards
        if card["waiting"]:
            operator.wait()
        else:
            step += 1
    raise AssertionError("the run never finished")


async def test_a_run_plays_a1_one_card_at_a_time(api: Api, keys: tuple[str, str]) -> None:
    async with runner(api, keys) as client:
        started = await start(client)
        assert started.status_code == 201, started.text
        run = started.json()

        cards = await play_through(client, run["run_id"], Operator(api, []))

    assert run["scenario"] == "A1"
    assert len(run["run_id"]) >= 32  # unguessable
    assert cards[0]["call"]["name"] == "POST /v1/mandates"
    decided = cards[1]
    assert decided["call"] == {
        "kind": "tool",
        "name": "request_decision",
        "request": decided["call"]["request"],
        "response": decided["call"]["response"],
    }
    assert decided["decision"]["outcome"] == "allow"
    assert decided["decision"]["audit_record_id"]
    assert cards[-1]["finished"] and cards[-1]["mismatches"] == []


async def test_a_decision_card_carries_its_rule_results(api: Api, keys: tuple[str, str]) -> None:
    async with runner(api, keys) as client:
        run_id = (await start(client)).json()["run_id"]
        cards = await play_through(client, run_id, Operator(api, []))

    rules = cards[1]["decision"]["rule_results"]
    assert rules, "the inspector shows every rule that ran"
    assert all(rule["rule_id"] and rule["result"] for rule in rules)


async def test_verify_replays_the_runs_decision_and_walks_the_chain(
    api: Api, keys: tuple[str, str]
) -> None:
    async with runner(api, keys) as client:
        run_id = (await start(client)).json()["run_id"]
        cards = await play_through(client, run_id, Operator(api, []))

        checked = await client.post(f"/runs/{run_id}/verify")

    assert checked.status_code == 200, checked.text
    body = checked.json()
    assert body["replay"]["audit_record_id"] == cards[1]["decision"]["audit_record_id"]
    assert (body["replay"]["status"], body["replay"]["identical"]) == ("identical", True)
    assert body["chain"]["clean"] is True
    assert body["chain"]["record_count"] >= 1
    assert body["chain"]["first_broken_seq"] is None


async def test_verify_needs_a_decision_and_a_live_run(api: Api, keys: tuple[str, str]) -> None:
    async with runner(api, keys) as client:
        run_id = (await start(client)).json()["run_id"]

        early = await client.post(f"/runs/{run_id}/verify")
        gone = await client.post("/runs/no-such-run/verify")

    assert (early.status_code, early.json()["code"]) == (409, "NO_DECISION_YET")
    assert (gone.status_code, gone.json()["code"]) == (404, "RUN_NOT_FOUND")


async def test_verify_is_limited_per_run(api: Api, keys: tuple[str, str]) -> None:
    async with runner(api, keys) as client:
        run_id = (await start(client)).json()["run_id"]
        await play_through(client, run_id, Operator(api, []))

        answers = [(await client.post(f"/runs/{run_id}/verify")).status_code for _ in range(11)]

    assert answers == [200] * 10 + [429]


async def test_status_reports_a_rail_outage_and_is_briefly_cached(
    api: Api, keys: tuple[str, str]
) -> None:
    clock = Seconds()
    async with runner(api, keys, seconds=clock) as client:
        before = (await client.get("/status")).json()
        started = api.post("/v1/demo/rail-down", {"Authorization": f"Bearer {keys[1]}"})
        assert started.status_code == 200, started.text
        cached = (await client.get("/status")).json()
        clock.at += 6
        during = (await client.get("/status")).json()

    assert before == {"rail_outage": False, "outage_ends_at": None}
    assert cached["rail_outage"] is False
    assert during["rail_outage"] is True
    assert during["outage_ends_at"] is not None


async def test_status_says_no_outage_when_the_api_cant_answer(
    api: Api, keys: tuple[str, str]
) -> None:
    broken = httpx.MockTransport(lambda _request: httpx.Response(500))
    async with runner(api, keys, api_transport=broken) as client:
        answer = await client.get("/status")

    assert answer.status_code == 200
    assert answer.json() == {"rail_outage": False, "outage_ends_at": None}


async def test_a_retried_next_returns_the_same_card_and_makes_one_decision(
    api: Api, keys: tuple[str, str]
) -> None:
    async with runner(api, keys) as client:
        run_id = (await start(client)).json()["run_id"]
        await client.post(f"/runs/{run_id}/next", json={"step": 0})

        first = await client.post(f"/runs/{run_id}/next", json={"step": 1})
        again = await client.post(f"/runs/{run_id}/next", json={"step": 1})

    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()


class LoseOneResponse(httpx.AsyncBaseTransport):
    """Sends every request; drops the answer to the first Decision, as a network might."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.lost = False
        self.decisions = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        if request.url.path == "/v1/decisions":
            self.decisions += 1
            if not self.lost:
                self.lost = True
                raise httpx.ReadTimeout("lost", request=request)
        return response


async def test_a_step_retried_after_a_lost_answer_reuses_its_idempotency_key(
    api: Api, keys: tuple[str, str]
) -> None:
    lossy = LoseOneResponse(transport_to(api))
    async with runner(api, keys, api_transport=lossy) as client:
        run_id = (await start(client)).json()["run_id"]
        await client.post(f"/runs/{run_id}/next", json={"step": 0})

        failed = await client.post(f"/runs/{run_id}/next", json={"step": 1})
        retried = await client.post(f"/runs/{run_id}/next", json={"step": 1})

    assert failed.status_code == 502
    assert failed.json()["code"] == "STEP_FAILED"
    assert retried.status_code == 200, retried.text
    assert lossy.decisions == 2
    # Both reached the API with one key, so there is one Decision, given back on the retry.
    decision_id = retried.json()["decision"]["decision_id"]
    agent = {"Authorization": f"Bearer {keys[0]}"}
    assert api.client.get(f"/v1/decisions/{decision_id}", headers=agent).status_code == 200


async def test_starts_past_the_per_ip_limit_get_429_with_retry_after(
    api: Api, keys: tuple[str, str]
) -> None:
    seconds = Seconds()
    async with runner(api, keys, seconds=seconds, demo_runner_runs_per_ip_per_minute=2) as one:
        assert (await start(one)).status_code == 201
        seconds.at += 20
        assert (await start(one)).status_code == 201

        refused = await start(one)

        seconds.at += 41  # the first start is now over a minute old
        allowed_again = await start(one)

    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == "40"
    assert allowed_again.status_code == 201


async def test_starts_past_the_runs_in_progress_limit_get_429(
    api: Api, keys: tuple[str, str]
) -> None:
    seconds = Seconds()
    settings: dict[str, Any] = {"seconds": seconds, "demo_runner_runs_in_progress_max": 1}
    async with runner(api, keys, **settings) as client:
        assert (await start(client)).status_code == 201

        refused = await start(client)

        seconds.at += 1801  # past the run TTL, the run no longer counts
        allowed_again = await start(client)

    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) > 0
    assert allowed_again.status_code == 201


async def test_asking_for_a_step_out_of_turn_is_409(api: Api, keys: tuple[str, str]) -> None:
    async with runner(api, keys) as client:
        run_id = (await start(client)).json()["run_id"]

        skipped = await client.post(f"/runs/{run_id}/next", json={"step": 2})

    assert skipped.status_code == 409
    assert skipped.json()["code"] == "WRONG_STEP"


async def test_an_unknown_run_or_scenario_is_404(api: Api, keys: tuple[str, str]) -> None:
    async with runner(api, keys) as client:
        no_scenario = await start(client, "Z9")
        no_run = await client.post("/runs/nope/next", json={"step": 0})

    assert (no_scenario.status_code, no_run.status_code) == (404, 404)


async def test_only_the_web_origin_may_call_the_runner(api: Api, keys: tuple[str, str]) -> None:
    preflight = {"Access-Control-Request-Method": "POST"}
    async with runner(api, keys) as client:
        ours = await client.options("/runs", headers={"Origin": WEB, **preflight})
        theirs = await client.options(
            "/runs", headers={"Origin": "https://evil.example", **preflight}
        )

    assert ours.headers.get("access-control-allow-origin") == WEB
    assert "access-control-allow-origin" not in theirs.headers


async def test_no_key_appears_in_a_card_a_log_or_an_error(
    api: Api, keys: tuple[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    lossy = LoseOneResponse(transport_to(api))
    async with runner(api, keys, api_transport=lossy) as client:
        run_id = (await start(client)).json()["run_id"]
        bodies = [(await client.post(f"/runs/{run_id}/next", json={"step": 0})).text]
        bodies.append((await client.post(f"/runs/{run_id}/next", json={"step": 1})).text)
        bodies += [json.dumps(card) for card in await play_through_from(client, run_id, 1, api)]

    seen = "\n".join(bodies) + caplog.text
    assert "STEP_FAILED" in seen  # the error is in there
    for key in keys:
        assert key not in seen


async def play_through_from(
    client: httpx.AsyncClient, run_id: str, step: int, api: Api
) -> list[Any]:
    operator = Operator(api, [])
    cards: list[Any] = []
    for _ in range(40):
        card = (await client.post(f"/runs/{run_id}/next", json={"step": step})).json()
        cards.append(card)
        if card["finished"]:
            break
        if card["waiting"]:
            operator.wait()
        else:
            step += 1
    return cards


def test_the_runner_refuses_a_live_key(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    live = "apg_live_" + "x" * 43
    monkeypatch.setenv("AGENTPAY_DEMO_AGENT_KEY", live)
    monkeypatch.setenv("AGENTPAY_DEMO_TENANT_KEY", live)

    with pytest.raises(ValidationError, match="only test keys"):
        runner_settings((live, "apg_test_" + "y" * 43))
    code = main([])

    err = capsys.readouterr().err
    assert code == 1
    assert "only test keys" in err
    assert live not in err


async def test_a_payer_step_waits_on_its_consent_link_until_asha_answers(
    api: Api, keys: tuple[str, str]
) -> None:
    async with runner(api, keys) as client:
        run_id = (await start(client, "E1")).json()["run_id"]
        await client.post(f"/runs/{run_id}/next", json={"step": 0})
        await client.post(f"/runs/{run_id}/next", json={"step": 1})  # step_up

        first = (await client.post(f"/runs/{run_id}/next", json={"step": 2})).json()
        again = (await client.post(f"/runs/{run_id}/next", json={"step": 2})).json()
        token = first["link"].split("#", 1)[1]
        tapped = api.client.post(
            "/v1/consent-link/approve", headers={"Consent-Link": token, "Idempotency-Key": "tap-1"}
        )
        answered = (await client.post(f"/runs/{run_id}/next", json={"step": 2})).json()

    assert first["waiting"] and first["actor"] == "payer"
    assert "/consent#" in first["link"]
    assert again["waiting"] and again["link"] == first["link"]  # one link, not a new one
    assert tapped.status_code == 200
    assert not answered["waiting"]
    assert answered["consent"]["status"] == "approved"


async def test_a_blocked_approval_shows_why_on_the_payers_card(
    api: Api, keys: tuple[str, str]
) -> None:
    async with runner(api, keys) as client:
        run_id = (await start(client, "E4")).json()["run_id"]
        for step in (0, 1, 2):  # setup, step_up, the Mandate revoked
            await client.post(f"/runs/{run_id}/next", json={"step": step})
        asking = (await client.post(f"/runs/{run_id}/next", json={"step": 3})).json()
        token = asking["link"].split("#", 1)[1]
        api.client.post(
            "/v1/consent-link/approve", headers={"Consent-Link": token, "Idempotency-Key": "tap-1"}
        )
        answered = (await client.post(f"/runs/{run_id}/next", json={"step": 3})).json()

    assert answered["consent"]["status"] == "blocked"
    assert answered["consent"]["reason_codes"][0] == "MANDATE_REVOKED"  # the primary reason


async def test_fast_forward_waits_for_the_visitors_press(api: Api, keys: tuple[str, str]) -> None:
    async with runner(api, keys) as client:
        run_id = (await start(client, "E3")).json()["run_id"]
        for step in (0, 1, 2):  # setup, step_up, Asha ignores the link
            card = (await client.post(f"/runs/{run_id}/next", json={"step": step})).json()
            assert not card["waiting"], card
        asking = (await client.post(f"/runs/{run_id}/next", json={"step": 3})).json()
        still = (await client.post(f"/runs/{run_id}/next", json={"step": 3})).json()
        pressed = (
            await client.post(f"/runs/{run_id}/next", json={"step": 3, "press": True})
        ).json()

    assert asking["waiting"] and asking["fast_forward"] == "Skip 10 minutes"
    assert still["waiting"]
    assert not pressed["waiting"]
    assert pressed["call"]["name"].startswith("POST /v1/demo/consent-requests/")
    assert pressed["consent"]["status"] == "expired"


class RunnerSender:
    """The worker's Tenant webhook sender, posting to the runner's receiver in-process."""

    def __init__(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        self.client = TestClient(app)

    def post(self, url: str, body: bytes, headers: Mapping[str, str]) -> int:
        assert url == RECEIVER
        return self.client.post("/webhooks", content=body, headers=dict(headers)).status_code


def _signed(
    secret: str, api: Api, event_type: str = "payment.captured"
) -> tuple[bytes, dict[str, str]]:
    raw = body("evt_x", event_type, api.clock.now(), {"payment_id": "pay_x"})
    sent = int(api.clock.now().timestamp())
    return raw, {SIGNATURE_HEADER: signature(secret, sent, raw), EVENT_ID_HEADER: "evt_x"}


async def test_the_receiver_keeps_a_signed_webhook_and_refuses_any_other(
    api: Api, keys: tuple[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    secret = "whsec_runner_test"  # noqa: S105 - a test-only signing secret
    raw, headers = _signed(secret, api)
    async with runner(api, keys, demo_runner_webhook_secret=SecretStr(secret)) as client:
        kept = await client.post("/webhooks", content=raw, headers=headers)
        forged = await client.post(
            "/webhooks", content=raw, headers={**headers, SIGNATURE_HEADER: "t=1,v1=00"}
        )
        unsigned = await client.post("/webhooks", content=raw)

    assert kept.status_code == 200, kept.text
    assert (forged.status_code, forged.json()["code"]) == (400, "BAD_SIGNATURE")
    assert unsigned.status_code == 400
    assert secret not in caplog.text and "pay_x" not in caplog.text


async def test_without_a_webhook_secret_there_is_no_receiver_and_h8_says_so(
    api: Api, keys: tuple[str, str]
) -> None:
    raw, headers = _signed("whsec_any", api)
    async with runner(api, keys) as client:
        posted = await client.post("/webhooks", content=raw, headers=headers)
        started = await start(client, "H8")

    assert posted.status_code == 404
    assert (started.status_code, started.json()["code"]) == (409, "WEBHOOKS_OFF")


async def test_h8_shows_each_signed_webhook_the_run_was_sent(
    api: Api, keys: tuple[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    secret = register_receiver(api, keys[1])
    app = create_app(
        runner_settings(keys, demo_runner_webhook_secret=SecretStr(secret)),
        transport=transport_to(api),
        now=api.clock.now,
        seconds=Seconds(),
    )
    operator = Operator(api, [], RunnerSender(app))
    browser = httpx.ASGITransport(app=app, client=("203.0.113.7", 50000))
    async with httpx.AsyncClient(transport=browser, base_url="http://runner.test") as client:
        run_id = (await start(client, "H8")).json()["run_id"]
        cards = await play_through_as_asha(client, run_id, operator)

    assert cards[-1]["finished"] and cards[-1]["mismatches"] == []
    shown = [c for c in cards if c["actor"] == "webhooks" and not c["waiting"]]
    last = shown[-1]["webhooks"]
    assert [e["event_type"] for e in last] == [
        "consent_request.created",
        "consent_request.resolved",
        "payment.captured",
        "dispute.in_review",
    ]
    assert {e["signature"] for e in last} == {"verified"}
    assert secret not in json.dumps(cards) and secret not in caplog.text


async def play_through_as_asha(
    client: httpx.AsyncClient, run_id: str, operator: Operator
) -> list[Any]:
    """Play every step, approving on the Consent link as Asha when a Payer step waits."""
    cards: list[Any] = []
    step = 0
    for _ in range(60):
        card = (await client.post(f"/runs/{run_id}/next", json={"step": step})).json()
        cards.append(card)
        if card.get("finished"):
            return cards
        if card.get("waiting"):
            if card["actor"] == "payer" and card["link"]:
                token = card["link"].split("#", 1)[1]
                operator.api.client.post(
                    "/v1/consent-link/approve",
                    headers={"Consent-Link": token, "Idempotency-Key": f"tap-{token[:8]}"},
                )
            operator.wait()
        else:
            step += 1
    raise AssertionError(f"the run never finished: {cards[-1]}")
