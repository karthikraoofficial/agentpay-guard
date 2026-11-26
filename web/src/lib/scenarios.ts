// The Scenario catalogue, as scripts/sync-scenarios.mjs copied it from demo/scenarios/
// after checking each file against demo/scenario.schema.json. Only the fields the page
// reads are typed here; the schema is the full format.
import data from "@/data/scenarios.json";

export type ChatLine = { speaker: "asha" | "shopbot" | "dealhunter"; text: string };

// Who says a chat line: Asha, ShopBot, or (B6) DealHunter, an Agent she never authorised.
export const SPEAKER_NAMES: Record<ChatLine["speaker"], string> = {
  asha: "Asha",
  shopbot: "ShopBot",
  dealhunter: "DealHunter",
};

export type Scenario = {
  id: string;
  title: string;
  group: string;
  playlists: string[];
  story: string;
  steps: unknown[];
  what_to_notice: string;
};

export const SCENARIOS: Scenario[] = (data as { scenarios: Scenario[] }).scenarios.map((s) => ({
  ...s,
  playlists: s.playlists ?? [],
}));

// The groups in docs/demo-journeys.md.
export const GROUPS: { id: string; name: string }[] = [
  { id: "A", name: "The everyday yes" },
  { id: "B", name: "Hard limits (always a block)" },
  { id: "C", name: "Warning signs (ask Asha first)" },
  { id: "D", name: "Does the cart match what Asha asked for?" },
  { id: "E", name: "Asking Asha (step-up consent)" },
  { id: "F", name: "Paying, and when the payment goes wrong" },
  { id: "G", name: "Disputes" },
  { id: "H", name: "Refunds, proof, and keeping the business informed" },
];
