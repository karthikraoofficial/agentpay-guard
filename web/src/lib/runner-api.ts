// The Demo runner (ADR-0031): the /demo page starts a run for a Scenario and asks for one
// step at a time. The runner holds the Demo Tenant's keys; the browser never sees one.
import type { ChatLine } from "@/lib/scenarios";

const RUNNER_URL = process.env.NEXT_PUBLIC_DEMO_RUNNER_URL ?? "http://localhost:8100";

export type Json = Record<string, unknown>;

export type Card = {
  step: number;
  actor: "agent" | "tenant" | "payer" | "reviewer" | "visitor" | "webhooks";
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
  // A cart check step: the live verdict, and the one the Scenario scripted for CI.
  // `differs` when they disagree: the run carries on with the live answer.
  intent: {
    intent_id: string;
    verdict: IntentVerdict;
    // Why it came back unknown without the model's word, e.g. "capped" (Model spend cap).
    failure_cause: string | null;
    scripted: IntentVerdict | null;
    differs: boolean;
  } | null;
  // A Dispute step: the Dispute as the step left it, with its latest Evidence pack in brief.
  dispute: DisputeView | null;
  // A verify-pack step: every pack version against the Audit chain and its Sign-off.
  pack_check: PackCheck | null;
  // A Payer step: the Consent link Asha answers on (the consent screen, ADR-0008). A
  // reviewer step: the Review link the reviewer acts on (the dispute viewer).
  link: string | null;
  // The Consent request as this step left it.
  consent: { consent_request_id: string; status: ConsentStatus; reason_codes: string[] | null } | null;
  // The error code of a refusal the journey shows, e.g. INSUFFICIENT_SCOPE.
  refused: string | null;
  // A fast-forward waiting for the visitor's press: the button's words (ADR-0032).
  fast_forward: string | null;
  // That control forces a failure (a switch) rather than ending a timer.
  switch: boolean;
  // A webhooks step: every Tenant webhook about this run that the Demo runner's receiver
  // checked and kept, in the order they arrived (H8).
  webhooks: ReceivedWebhook[] | null;
  waiting: boolean;
  finished: boolean;
  mismatches: string[];
};

// A Tenant webhook as the receiver kept it. Only deliveries whose signature verified are
// kept. `data` holds ids, statuses, codes and times only.
export type ReceivedWebhook = {
  event_id: string;
  event_type: string;
  created_at: string;
  data: Json;
  signed_at: string;
  signature: "verified";
};

export type DisputeView = {
  dispute_id: string;
  status: "open" | "in_review" | "ready" | "expired" | "withdrawn";
  pack_version: number | null;
  pack_hash: string | null;
  evidence_strength: "strong" | "mixed" | "weak" | null;
  // The network reason codes to file under, and the Evidence facts a strong pack would
  // need that don't hold.
  visa_code: string | null;
  mastercard_code: string | null;
  missing_evidence: string[];
  // Who wrote the narrative, and why code stood in for the model, if it did.
  narrative_source: "model" | "template" | null;
  narrative_note: string | null;
  signed_off_version: number | null;
  reviewer: string | null;
};

export type PackCheck = {
  dispute_id: string;
  status: "clean" | "different" | "pack_problem";
  clean: boolean;
  versions: { version: number; signed_off: boolean; problems: string[] }[];
  pack_problems: string[];
};

export type IntentVerdict = "match" | "mismatch" | "unknown";

export type ConsentStatus ="pending" | "approved" | "declined" | "expired" | "blocked";

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
  // verify-pack, when the run has a Dispute.
  pack: PackCheck | null;
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

export type DemoStatus = { rail_outage: boolean; outage_ends_at: string | null };

// For the banner. Any failure reads as no outage: the page itself still works.
export async function demoStatus(): Promise<DemoStatus> {
  try {
    const response = await fetch(`${RUNNER_URL}/status`, { cache: "no-store" });
    if (response.ok) return (await response.json()) as DemoStatus;
  } catch {
    // no banner
  }
  return { rail_outage: false, outage_ends_at: null };
}
