"""The demo CLI (`python -m demo`, #138, #288): it plays one Scenario (`--scenario`) or a
playlist (`--playlist`) over HTTP through the step engine, here against the in-process app
on the mock rail with mock settle on. The Scenario tests in test_scenarios.py prove each
journey; these prove the CLI around them."""

import pytest

from demo import engine
from demo.__main__ import main, run_playlist
from demo.playlist import Playlist, load_playlists
from demo.scenario import catalogue
from tests.api.conftest import Api
from tests.conftest import make_settings
from tests.demo.conftest import clients
from tests.demo.operator import Operator

KEYS = {"demo_agent_key": "apg_test_a", "demo_tenant_key": "apg_test_b"}


def script_the_model(api: Api, playlist: Playlist) -> None:
    """The fake model gateway answers each journey's Intent checks with its scripted
    verdicts, in the order the playlist plays them."""
    for journey in playlist.journeys:
        verdicts = catalogue()[journey].scripted_verdicts().values()
        api.gateway.script.extend(v.upper() for v in verdicts)


@pytest.mark.anyio
async def test_the_cli_plays_a_playlist_journey_by_journey(api: Api, keys: tuple[str, str]) -> None:
    out: list[str] = []
    pitch = load_playlists()["pitch"]
    script_the_model(api, pitch)
    async with clients(api, keys) as (agent, tenant):
        code = await run_playlist(
            pitch,
            agent=agent,
            tenant=tenant,
            say=out.append,
            wait=Operator(api, out).wait,
            now=api.clock.now,
            patience=20,
            people="engine",  # D2's Asha declines; a person at the terminal would
        )

    text = "\n".join(out)
    assert code == 0, text
    assert out[0] == "Playlist pitch: The 90-second pitch (5 journeys)"
    started = [line for line in out if line.startswith("Journey ")]
    assert started == [f"Journey {i} of 5: {j}" for i, j in enumerate(pitch.journeys, 1)]
    assert out[-1] == "Playlist pitch: all 5 journeys as expected."
    assert keys[0] not in text and keys[1] not in text


@pytest.mark.anyio
async def test_a_playlist_stops_at_the_first_journey_not_as_expected(
    api: Api, keys: tuple[str, str]
) -> None:
    out: list[str] = []
    playlist = Playlist(
        id="payments-pm",
        title="Two",
        audience="a test",
        length="short",
        story="H1, A1",
        journeys=["H1", "A1"],
    )
    async with clients(api, keys) as (agent, tenant):
        code = await run_playlist(
            playlist,
            agent=agent,
            tenant=tenant,
            say=out.append,
            wait=lambda: None,  # no worker settles H1's Payment
            now=api.clock.now,
            patience=3,
        )

    assert code == 1
    assert not any(line.startswith("Scenario A1") for line in out)
    assert out[-1] == "Playlist payments-pm stopped at journey 1 of 2, H1."


def test_every_playlist_plays_from_the_cli() -> None:
    # Only the Demo runner receives Tenant webhooks (H8), so no playlist may need them.
    for playlist in load_playlists().values():
        needing = [j for j in playlist.journeys if engine.needs_webhooks(catalogue()[j])]
        assert needing == [], f"{playlist.id} plays {needing}, which the CLI refuses"


def test_the_cli_refuses_an_unknown_playlist(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["--base-url", "http://localhost:8000", "--playlist", "nope"], make_settings(**KEYS)
    )

    assert code == 2
    err = capsys.readouterr().err
    assert "No playlist nope" in err
    assert "pitch" in err  # it lists the ones there are


def test_the_cli_needs_a_scenario_or_a_playlist() -> None:
    with pytest.raises(SystemExit) as exited:
        main(["--base-url", "http://localhost:8000"], make_settings(**KEYS))

    assert exited.value.code == 2


def test_the_numbered_journeys_are_gone() -> None:
    with pytest.raises(SystemExit) as exited:
        main(["--base-url", "http://localhost:8000", "--journey", "1"], make_settings(**KEYS))

    assert exited.value.code == 2


def test_the_cli_refuses_to_start_without_the_demo_keys(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--base-url", "http://localhost:8000", "--scenario", "A1"], make_settings())

    assert code == 2
    assert "AGENTPAY_DEMO_AGENT_KEY" in capsys.readouterr().err
