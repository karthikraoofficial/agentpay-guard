"""Playlists (#287): journeys strung together for one audience, as `docs/demo-journeys.md`
section 7 lists them. One JSON file per playlist in `demo/playlists/`, named after its id,
listing the journey ids in the order they play.

Every journey a playlist names must have a Scenario file, and a playlist must agree with
the Scenarios' own `playlists` tags: each journey it plays is tagged with it, and each
Scenario tagged with it is in it. Either mismatch fails CI. The web app's /demo page and
the home page's "Try the demo" read the same files.

`demo/playlist.schema.json` is generated from this model with the Scenario schema
(`uv run python -m demo.scenario --write-schema`), and a test fails when it is stale.
"""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from demo.scenario import Playlist as PlaylistId
from demo.scenario import Scenario, ScenarioId, Text, catalogue

PLAYLISTS = Path(__file__).parent / "playlists"
PLAYLIST_SCHEMA = Path(__file__).parent / "playlist.schema.json"


class Playlist(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: PlaylistId
    title: Text
    # Who it is for, and roughly how long it takes to watch.
    audience: Text
    length: Text
    # The story it tells, in one or two sentences.
    story: Text
    journeys: list[ScenarioId] = Field(min_length=1)

    @model_validator(mode="after")
    def _each_journey_once(self) -> "Playlist":
        seen: set[str] = set()
        for journey in self.journeys:
            if journey in seen:
                raise ValueError(f"{journey} appears more than once")
            seen.add(journey)
        return self


def _agrees(playlist: Playlist, scenarios: dict[str, Scenario]) -> None:
    for journey in playlist.journeys:
        scenario = scenarios.get(journey)
        if scenario is None:
            raise ValueError(f"playlist {playlist.id} names {journey}, which has no Scenario file")
        if playlist.id not in scenario.playlists:
            raise ValueError(
                f"{journey} isn't tagged {playlist.id}, but playlist {playlist.id} plays it"
            )
    for scenario in scenarios.values():
        if playlist.id in scenario.playlists and scenario.id not in playlist.journeys:
            raise ValueError(
                f"{scenario.id} is tagged {playlist.id}, but playlist {playlist.id} doesn't play it"
            )


def load_playlists(
    root: Path = PLAYLISTS, scenarios: dict[str, Scenario] | None = None
) -> dict[str, Playlist]:
    """Every playlist file, by id, each checked against the Scenario catalogue."""
    known = catalogue() if scenarios is None else scenarios
    found: dict[str, Playlist] = {}
    for path in sorted(root.glob("*.json")):
        playlist = Playlist.model_validate_json(path.read_text(encoding="utf-8"))
        if playlist.id != path.stem:
            raise ValueError(
                f"{path.name} holds playlist {playlist.id}; name it {playlist.id}.json"
            )
        _agrees(playlist, known)
        found[playlist.id] = playlist
    return found


def playlist_schema_text() -> str:
    return json.dumps(Playlist.model_json_schema(), indent=2, sort_keys=True) + "\n"
