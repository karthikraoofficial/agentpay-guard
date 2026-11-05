// The Eval report as evals/report.py writes it, plus where the Ops console's copy came
// from (scripts/sync-eval-report.mjs). Read at build time only: no API key, no live data.
import synced from "@/data/eval-report.json";

export type OutcomeCounts = {
  cases: number;
  allow: number;
  step_up: number;
  block: number;
};

export type FileEntry = {
  file: string;
  kind: "decision" | "dispute" | "narrative";
  pack: string | null;
  cases: number;
  mismatches: number;
  allow: number | null;
  step_up: number | null;
  block: number | null;
  false_allow: number | null;
  false_block: number | null;
  wrong_reason: number | null;
};

export type EvalReport = {
  about: string;
  decision_cases: number;
  false_allow: number;
  false_block: number;
  wrong_reason: number;
  mismatches: number;
  step_up_rate: number;
  files: FileEntry[];
  scenarios: Record<string, OutcomeCounts>;
};

export type ReportSource = {
  // True when the committed report wasn't reachable and the fixture was used.
  sample: boolean;
  // `git describe` of the checkout the report came from: a tag, or a tag plus commit.
  release: string | null;
};

export const evalReport = synced.report as EvalReport;
export const reportSource = synced.source as ReportSource;
