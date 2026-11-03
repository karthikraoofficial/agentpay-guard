// The Review link API. As with Consent links, the token is the only credential: it comes
// from the page URL's #fragment, which browsers never send to a server, and goes to the
// API in the `Review-Link` header, never in a path.

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type Money = { minor: number; currency: string };

export type Reviewer = { id: string; name: string };

export type Strength = "strong" | "mixed" | "weak";

export type Assessment = {
  reason: string;
  visa_code: string;
  mastercard_code: string;
  strength: Strength;
  facts: string[];
  missing: string[];
  notes: string[];
};

export type Claim = { text: string; cites: string[]; kind: string | null };

// An Audit record as the bundle carries it. `record` is the record itself; the viewer
// shows its parts as they are, so its shape is left open here.
export type BundleRecord = {
  audit_record_id: string;
  seq: number;
  kind: string;
  record_hash: string;
  record: Record<string, unknown> & { recorded_at: string };
};

export type Pack = {
  version: number;
  bundle_hash: string;
  bundle: {
    trail: BundleRecord[];
    related: BundleRecord[];
    payer_payments?: BundleRecord[];
  };
  narrative: Claim[];
  narrative_source: "model" | "template";
  narrative_note: string | null;
  built_at: string;
  policy_pack: { id: string; version: string; hash: string } | null;
  assessment: Assessment | null;
};

export type Dispute = {
  dispute_id: string;
  payment_id: string;
  decision_id: string;
  reason: string;
  status: "open" | "in_review" | "ready" | "expired" | "withdrawn";
  respond_by: string;
  sign_off: {
    reviewer: Reviewer;
    pack_version: number;
    pack_hash: string;
    signed_off_at: string;
  } | null;
  pack: Pack | null;
  pack_hash: string | null;
};

export type ReviewView = { reviewer: Reviewer; dispute: Dispute };

// A refusal keeps the API's stable `code`, so the viewer can say what went wrong.
export type Result<T> =
  | { ok: true; data: T }
  | { ok: false; invalid: boolean; code: string | null };

async function call<T>(path: string, init: RequestInit): Promise<Result<T>> {
  try {
    const response = await fetch(`${API_URL}${path}`, { ...init, cache: "no-store" });
    if (response.ok) return { ok: true, data: (await response.json()) as T };
    const problem = (await response.json().catch(() => null)) as { code?: string } | null;
    return { ok: false, invalid: response.status === 404, code: problem?.code ?? null };
  } catch {
    return { ok: false, invalid: false, code: null };
  }
}

export function viewReview(token: string): Promise<Result<ReviewView>> {
  return call("/v1/review-link", { headers: { "Review-Link": token } });
}

// `idempotencyKey` is made once per action, so a double click or a retry after a
// dropped connection acts once and gets the same answer back.
export function signOff(
  token: string,
  packVersion: number,
  idempotencyKey: string,
): Promise<Result<Dispute>> {
  return call("/v1/review-link/sign-off", {
    method: "POST",
    headers: {
      "Review-Link": token,
      "Idempotency-Key": idempotencyKey,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ pack_version: packVersion }),
  });
}

export function regenerate(token: string, idempotencyKey: string): Promise<Result<Dispute>> {
  return call("/v1/review-link/regenerate", {
    method: "POST",
    headers: { "Review-Link": token, "Idempotency-Key": idempotencyKey },
  });
}

export const REASON_TEXT: Record<string, string> = {
  unauthorised: "Unauthorised: the Payer says they never agreed to this",
  not_as_described: "Not as described",
  not_received: "Not received",
  duplicate: "Duplicate payment",
  amount_differs: "Amount differs",
  cancelled_recurring: "Cancelled recurring payment",
};

export const FACT_TEXT: Record<string, string> = {
  decision_within_mandate: "The payment was allowed under the Mandate, or the Payer approved it",
  payer_intent_confirmed: "An Intent match or an attested approval shows the Payer wanted it",
  intent_mismatch_approved: "The Payer approved a purchase the Intent check said didn't match",
  mandate_revoked_before_capture: "The Mandate was revoked before the payment was captured",
  mandate_active_at_decision: "The Mandate was active when the Agent asked",
  mandate_revoked_before_decision: "The Mandate was revoked before the Agent asked",
  amount_matches_decision: "The captured amount is the amount that was allowed",
  amount_differs_from_decision: "The captured amount differs from the amount allowed",
  within_quoted_total: "The amount is within the Quoted total the Agent was given",
  no_duplicate_payment: "No other payment of this amount to this merchant within the window",
  duplicate_payment_found: "Another payment of this amount to this merchant within the window",
  fulfilment_evidence: "Evidence of delivery from the merchant",
};

// Refusals the reviewer can act on; anything else is a generic failure.
export const REFUSAL_TEXT: Record<string, string> = {
  PACK_VERSION_NOT_CURRENT: "A newer version of this pack exists. Reload to review it.",
  DISPUTE_NOT_IN_REVIEW: "The pack is being rebuilt. Wait for the new version, then sign off.",
  PACK_VERSION_LIMIT: "This dispute already has as many pack versions as it may have.",
  DISPUTE_CLOSED: "This dispute is closed.",
};
