"""`uv run python -m demo --base-url <api>`: play demo journeys against a deployment.

`--scenario <id>` plays one Scenario from the catalogue (demo/scenarios/), and
`--playlist <id>` plays a playlist (demo/playlists/) journey by journey, stopping at the
first that doesn't end as expected. Both go through the step engine the Demo runner uses.
H8 (Tenant webhooks) needs the Demo runner's receiver, so the CLI refuses it.

The Demo Tenant's keys come from settings (AGENTPAY_DEMO_AGENT_KEY and
AGENTPAY_DEMO_TENANT_KEY), never from the command line, and are never printed. The
deployment must use the mock rail; journeys that wait for the bank also need mock settle
on and a worker."""

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from functools import partial

import anyio
import httpx

from core.settings import Settings, get_settings
from demo import engine
from demo.playlist import Playlist, load_playlists
from demo.scenario import Scenario, catalogue


def main(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m demo", description=__doc__)
    parser.add_argument("--base-url", required=True, help="the API, e.g. https://api.example")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--scenario", help="one Scenario from the catalogue, e.g. A1")
    which.add_argument("--playlist", help="a playlist, e.g. pitch")
    parser.add_argument("--interval", type=float, default=3.0, help="seconds between checks")
    parser.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for a step")
    args = parser.parse_args(argv)

    settings = settings or get_settings()
    if settings.demo_agent_key is None or settings.demo_tenant_key is None:
        print(
            "Set AGENTPAY_DEMO_AGENT_KEY and AGENTPAY_DEMO_TENANT_KEY (in .env) to the agent key "
            "and tenant key `python -m api.admin create-tenant` printed for the Demo Tenant.",
            file=sys.stderr,
        )
        return 2

    playlist: Playlist | None = None
    if args.playlist is not None:
        playlists = load_playlists()
        playlist = playlists.get(args.playlist)
        if playlist is None:
            print(
                f"No playlist {args.playlist}. There are: {', '.join(sorted(playlists))}.",
                file=sys.stderr,
            )
            return 2
        scenarios = [catalogue()[journey] for journey in playlist.journeys]
    else:
        scenario = catalogue().get(args.scenario)
        if scenario is None:
            print(f"No Scenario {args.scenario}.", file=sys.stderr)
            return 2
        scenarios = [scenario]
    for scenario in scenarios:
        if engine.needs_webhooks(scenario):
            print(
                f"Scenario {scenario.id} shows Tenant webhooks, which only the Demo runner "
                "receives: play it on the web app's /demo page.",
                file=sys.stderr,
            )
            return 2

    agent_key = settings.demo_agent_key.get_secret_value()
    tenant_key = settings.demo_tenant_key.get_secret_value()

    async def go() -> int:
        async with (
            _client(args.base_url, agent_key) as agent,
            _client(args.base_url, tenant_key) as tenant,
        ):
            play = (
                partial(run_playlist, playlist)
                if playlist is not None
                else partial(run_scenario, scenarios[0])
            )
            return await play(
                agent=agent,
                tenant=tenant,
                say=lambda line: print(line, flush=True),
                wait=lambda: time.sleep(args.interval),
                now=lambda: datetime.now(UTC),
                patience=max(1, int(args.timeout / args.interval)),
            )

    return anyio.run(go)


def _client(base_url: str, key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url, headers={"Authorization": f"Bearer {key}"}, timeout=30.0
    )


_SPEAKERS = {"asha": "Asha", "shopbot": "ShopBot"}


def _describe(card: engine.Card) -> str:
    call = card.call
    if call is None:
        return f"step {card.step}"
    if card.refused is not None:
        return f"{call.name}: refused, {card.refused}"
    if card.actor == "payer" and card.consent is not None:
        blocked = card.consent.reason_codes
        why = f" ({', '.join(blocked)})" if blocked else ""
        return f"Asha's answer: {card.consent.status}{why}"
    if card.decision is not None:
        d = card.decision
        reasons = ", ".join(d.reason_codes) or "none"
        return (
            f"{call.name}: {d.outcome} (Reason codes: {reasons}; Decision {d.decision_id}, "
            f"pack {d.policy_pack}, Audit record {d.audit_record_id})"
        )
    if card.intent is not None:
        i = card.intent
        line = f"{call.name}: {i.verdict}" + (f", {i.failure_cause}" if i.failure_cause else "")
        if i.scripted is not None:
            line += f" (scripted: {i.scripted})"
        if i.differs:
            line += "; the live verdict differs from the script, so the run carries on with it"
        return line
    if card.pack_check is not None:
        p = card.pack_check
        signed = [str(v.version) for v in p.versions if v.signed_off and not v.problems]
        matches = f" (v{', v'.join(signed)} matches its Sign-off)" if signed else ""
        problems = [f"v{v.version}: {'; '.join(v.problems)}" for v in p.versions if v.problems]
        found = f" ({'; '.join(problems)})" if problems else ""
        return f"verify-pack: {p.status}{matches}{found}"
    if card.dispute is not None:
        ds = card.dispute
        line = f"{call.name}: Dispute {ds.dispute_id} {ds.status}"
        if ds.pack_version is not None:
            writer = {"model": "the model", "template": "code"}.get(str(ds.narrative_source))
            line += f", pack v{ds.pack_version}, strength {ds.evidence_strength or 'n/a'}"
            line += f", narrative by {writer or ds.narrative_source}"
            if ds.narrative_note:
                line += f" ({ds.narrative_note})"
        if ds.signed_off_version is not None:
            line += f", v{ds.signed_off_version} signed off by {ds.reviewer}"
        return line
    response = call.response
    if card.consent is not None:
        return f"{call.name}: Consent request {card.consent.status}"
    # A resent call (F3): the answer every send got, and how many distinct answers came.
    resent = ""
    if "sends" in response and isinstance(response.get("answer"), dict):
        distinct = response["distinct_answers"]
        plural = "" if distinct == 1 else "s"
        resent = f" (sent {response['sends']} times, {distinct} distinct answer{plural})"
        response = response["answer"]
    # Most specific first: a Refund names its Payment, and a Payment its Mandate.
    for name in ("refund_id", "dispute_id", "payment_id", "mandate_id"):
        if name in response:
            status = response.get("status")
            return f"{call.name}: {response[name]}" + (f" {status}" if status else "") + resent
    return call.name + resent


async def run_scenario(
    scenario: Scenario,
    *,
    agent: httpx.AsyncClient,
    tenant: httpx.AsyncClient,
    say: Callable[[str], None],
    wait: Callable[[], None],
    now: Callable[[], datetime],
    patience: int,
    people: engine.People = "visitor",
) -> int:
    """Play a Scenario, printing each step: 0 if it ended as expected, 1 if not. With
    `people="visitor"` a person answers each Consent and Review link the CLI prints; with
    `"engine"` the engine answers as the Scenario scripts, as in CI."""
    say(f"Scenario {scenario.id}: {scenario.title}")
    run = engine.Run(scenario, agent=agent, tenant=tenant, now=now, people=people)
    told: set[int] = set()
    waits = 0
    while True:
        try:
            card = await run.next()
        except engine.StepFailed as failed:
            say(f"Failed at '{failed.step}': {failed.detail}")
            return 1
        if card.step not in told:
            told.add(card.step)
            for line in card.chat:
                say(f"  {_SPEAKERS[line.speaker]}: {line.text}")
            if card.link and card.waiting and card.actor == "reviewer":
                say(f"  - Review link (open it and act as the reviewer): {card.link}")
            elif card.link and card.waiting:
                say(f"  - Consent link (open it and answer as Asha): {card.link}")
        if card.fast_forward:
            # No button in a terminal: press fast-forward at once (a Demo control).
            say(f"  - {'Demo control' if card.switch else 'Fast-forward'}: {card.fast_forward}")
            try:
                card = await run.next(press=True)
            except engine.StepFailed as failed:
                say(f"Failed at '{failed.step}': {failed.detail}")
                return 1
        if card.waiting:
            waits += 1
            if waits > patience:
                say(f"Failed at '{_describe(card)}': still waiting after {patience} checks")
                return 1
            wait()
            continue
        waits = 0
        say(f"  - {_describe(card)}")
        if card.finished:
            break
    if card.mismatches:
        for mismatch in card.mismatches:
            say(f"Not as expected: {mismatch}")
        return 1
    say(f"As expected. What to notice: {scenario.what_to_notice}")
    return 0


async def run_playlist(
    playlist: Playlist,
    *,
    agent: httpx.AsyncClient,
    tenant: httpx.AsyncClient,
    say: Callable[[str], None],
    wait: Callable[[], None],
    now: Callable[[], datetime],
    patience: int,
    people: engine.People = "visitor",
) -> int:
    """Play a playlist's journeys in order, as `run_scenario` plays each: 0 if every one
    ended as expected, 1 at the first that didn't (the rest don't play)."""
    count = len(playlist.journeys)
    say(f"Playlist {playlist.id}: {playlist.title} ({count} journeys)")
    scenarios = catalogue()
    for number, journey in enumerate(playlist.journeys, 1):
        say("")
        say(f"Journey {number} of {count}: {journey}")
        code = await run_scenario(
            scenarios[journey],
            agent=agent,
            tenant=tenant,
            say=say,
            wait=wait,
            now=now,
            patience=patience,
            people=people,
        )
        if code != 0:
            say(f"Playlist {playlist.id} stopped at journey {number} of {count}, {journey}.")
            return 1
    say("")
    say(f"Playlist {playlist.id}: all {count} journeys as expected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
