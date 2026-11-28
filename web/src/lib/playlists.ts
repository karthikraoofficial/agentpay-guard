// Playlists (#287): journeys strung together for one audience, as scripts/sync-scenarios.mjs
// copied them from demo/playlists/ after checking each against demo/playlist.schema.json and
// the Scenario catalogue. The home page's "Try the demo" plays the 90-second pitch.
import data from "@/data/playlists.json";
import { SCENARIOS, type Scenario } from "@/lib/scenarios";

export type Playlist = {
  id: string;
  title: string;
  audience: string;
  length: string;
  story: string;
  journeys: string[];
};

export const PLAYLISTS: Playlist[] = (data as { playlists: Playlist[] }).playlists;

export const PITCH_ID = "pitch";

export function playlistById(id: string | null): Playlist | null {
  return PLAYLISTS.find((p) => p.id === id) ?? null;
}

// The playlist's journeys in order. The sync script has checked each one exists.
export function playlistScenarios(playlist: Playlist): Scenario[] {
  return playlist.journeys
    .map((id) => SCENARIOS.find((s) => s.id === id))
    .filter((s): s is Scenario => s !== undefined);
}
