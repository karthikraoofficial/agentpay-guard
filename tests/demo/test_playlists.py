"""Playlists (#287): one file per playlist in `demo/playlists/`, listing journey ids in the
order they play. A playlist that names a journey with no Scenario file fails CI, and so
does one that disagrees with the Scenarios' own playlist tags."""

import json
from pathlib import Path

import pytest

from demo.playlist import PLAYLIST_SCHEMA, load_playlists, playlist_schema_text

PITCH = json.loads(Path("demo/playlists/pitch.json").read_text(encoding="utf-8"))


def _write(folder: Path, playlist: dict[str, object]) -> Path:
    folder.mkdir(exist_ok=True)
    (folder / f"{playlist['id']}.json").write_text(json.dumps(playlist), encoding="utf-8")
    return folder


def test_the_committed_playlist_schema_is_the_models() -> None:
    assert PLAYLIST_SCHEMA.read_text(encoding="utf-8") == playlist_schema_text(), (
        "demo/playlist.schema.json is stale: run `uv run python -m demo.scenario --write-schema`"
    )


def test_the_pitch_plays_a1_b4_d2_e1_g1() -> None:
    pitch = load_playlists()["pitch"]

    assert pitch.journeys == ["A1", "B4", "D2", "E1", "G1"]


@pytest.mark.parametrize(
    ("playlist", "journeys"),
    [
        ("payments-pm", ["A2", "B1", "C1", "C4", "D6", "E3", "F3", "F5", "G4", "H1"]),
        ("risk-compliance", ["B4", "C3", "D5", "E4", "E5", "F7", "H4", "H5", "H6"]),
        ("engineering", ["C5", "C6", "D5", "F2", "F3", "F8", "F9", "G8", "H7"]),
        ("disputes", ["G1", "G2", "G3", "G6", "G7", "G9", "G11"]),
    ],
)
def test_each_audience_playlist_plays_the_journeys_section_7_lists(
    playlist: str, journeys: list[str]
) -> None:
    assert load_playlists()[playlist].journeys == journeys


def test_a_playlist_naming_a_journey_with_no_scenario_fails(tmp_path: Path) -> None:
    folder = _write(tmp_path / "p", {**PITCH, "journeys": ["A1", "Z9"]})

    with pytest.raises(ValueError, match="Z9"):
        load_playlists(folder)


def test_a_file_must_be_named_after_its_playlist(tmp_path: Path) -> None:
    folder = tmp_path / "p"
    folder.mkdir()
    (folder / "engineering.json").write_text(json.dumps(PITCH), encoding="utf-8")

    with pytest.raises(ValueError, match=r"name it pitch.json"):
        load_playlists(folder)


def test_a_journey_may_appear_only_once(tmp_path: Path) -> None:
    folder = _write(tmp_path / "p", {**PITCH, "journeys": ["A1", "A1"]})

    with pytest.raises(ValueError, match="more than once"):
        load_playlists(folder)


def test_a_playlist_agrees_with_the_scenarios_tags(tmp_path: Path) -> None:
    dropped = _write(tmp_path / "dropped", {**PITCH, "journeys": ["A1", "B4", "E1", "G1"]})
    stranger = _write(tmp_path / "stranger", {**PITCH, "journeys": [*PITCH["journeys"], "H1"]})

    with pytest.raises(ValueError, match="D2 is tagged pitch"):
        load_playlists(dropped)
    with pytest.raises(ValueError, match="H1 isn't tagged pitch"):
        load_playlists(stranger)
