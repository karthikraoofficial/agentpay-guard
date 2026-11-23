// The Demo runner (ADR-0031): the /demo page starts a run for a Scenario and asks for one
// step at a time. The runner holds the Demo Tenant's keys; the browser never sees one.
import type { ChatLine } from "@/lib/scenarios";

const RUNNER_URL = process.env.NEXT_PUBLIC_DEMO_RUNNER_URL ?? "http://localhost:8100";

export type Json = Record<string, unknown>;

export type Card = {
  step: number;
  actor: "agent" | "tenant" | "payer" | "reviewer" | "visitor";
  chat: ChatLine[];
  call: { kind: "tool" | "route"; name: string; request: Json | null; response: Json } | null;
  decision: {
    decision_id: string;
    outcome: "allow" | "step_up" | "block";
    reason_codes: string[];
    policy_pack: string;
    pack_hash: string;
    audit_record_id: string;
    // Every rule that ran. Empty if the Audit record couldn't be read.
    rule_results: { rule_id: string; result: string; reason_code: string | null }[];
  } | null;
  // A Payer step: the Consent link Asha answers on (the consent screen, ADR-0008).
  link: string | null;
  // The Consent request as this step left it.
  consent: { consent_request_id: string; status: ConsentStatus; reason_codes: string[] | null } | null;
  // The error code of a refusal the journey shows, e.g. INSUFFICIENT_SCOPE.
  refused: string | null;
  // A fast-forward waiting for the visitor's press: the button's words (ADR-0032).
  fast_forward: string | null;
  waiting: boolean;
  finished: boolean;
  mismatches: string[];
};

export type ConsentStatus = "pending" | "approved" | "declined" | "expired" | "blocked";

export type Started = { run_id: string; scenario: string; steps: number };

// The inspector's Verify / Replay button: both answers.
export type Verification = {
  replay: {
    audit_record_id: string;
    seq: number;
    status: "identical" | "different" | "nothing_to_replay" | "pack_problem";
    identical: boolean;
    difference: string | null;
    detail: string | null;
  };
  chain: { clean: boolean; record_count: number; first_broken_seq: number | null };
};

export type Failure =
  // Too many runs: from this visitor, or overall. Try again after `seconds`.
  | { kind: "busy"; seconds: number }
  // The run is gone (it expired, or the runner restarted): start again.
  | { kind: "gone" }
  // A step failed at the API; asking for the same step again retries it safely.
  | { kind: "failed"; detail: string }
  // The runner didn't answer.
  | { kind: "unreachable" };

export type Result<T> = { ok: true; data: T } | { ok: false; failure: Failure };

async function post<T>(path: string, body: Json): Promise<Result<T>> {
  let response: Response;
  try {
    response = await fetch(`${RUNNER_URL}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });
  } catch {
    return { ok: false, failure: { kind: "unreachable" } };
  }
  if (response.ok) return { ok: true, data: (await response.json()) as T };
  if (response.status === 429) {
    const seconds = Number(response.headers.get("Retry-After") ?? "30");
    return { ok: false, failure: { kind: "busy", seconds: Number.isFinite(seconds) ? seconds : 30 } };
  }
  if (response.status === 404) return { ok: false, failure: { kind: "gone" } };
  let detail = `The runner answered ${response.status}.`;
  try {
    const problem = (await response.json()) as { detail?: string };
    if (problem.detail) detail = problem.detail;
  } catch {
    // keep the status line
  }
  return { ok: false, failure: { kind: "failed", detail } };
}

export function startRun(scenario: string): Promise<Result<Started>> {
  return post("/runs", { scenario });
}

// `step` is the step the page means: asking again for the one just run gives its card
// back instead of running it twice, so a double click or a retry is safe.
// `press` is the visitor pressing the fast-forward this step is waiting on.
export function nextStep(runId: string, step: number, press = false): Promise<Result<Card>> {
  return post(`/runs/${encodeURIComponent(runId)}/next`, { step, press });
}

export function verifyRun(runId: string): Promise<Result<Verification>> {
  return post(`/runs/${encodeURIComponent(runId)}/verify`, {});
}
