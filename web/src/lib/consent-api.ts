// The Consent link API (ADR-0008). The token is the only credential: it comes from the
// page URL's #fragment, which browsers never send to a server, and goes to the API in
// the `Consent-Link` header, never in a path.

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type Money = { minor: number; currency: string };

export type ConsentView = {
  consent_request_id: string;
  status: "pending" | "approved" | "declined" | "expired" | "blocked";
  expires_at: string;
  payment: {
    amount: Money;
    amount_text: string;
    merchant_name: string;
    mcc: string;
    agent_name: string | null;
  };
  reasons: { code: string; text: string }[];
  intent_verdict: "match" | "mismatch" | "unknown" | null;
  mandate_inactive: boolean;
};

export type Resolution = {
  consent_request_id: string;
  status: "approved" | "declined" | "blocked";
  resolved_at: string;
  reason_codes: string[] | null;
};

export type Result<T> = { ok: true; data: T } | { ok: false; invalid: boolean };

async function call<T>(path: string, init: RequestInit): Promise<Result<T>> {
  try {
    const response = await fetch(`${API_URL}${path}`, { ...init, cache: "no-store" });
    if (response.ok) return { ok: true, data: (await response.json()) as T };
    return { ok: false, invalid: response.status === 404 };
  } catch {
    return { ok: false, invalid: false };
  }
}

export function viewConsent(token: string): Promise<Result<ConsentView>> {
  return call("/v1/consent-link", { headers: { "Consent-Link": token } });
}

// `idempotencyKey` is made once per decision, so a double tap or a retry after a dropped
// connection resolves the request once and gets the same answer back.
export function resolveConsent(
  token: string,
  action: "approve" | "decline",
  idempotencyKey: string,
): Promise<Result<Resolution>> {
  return call(`/v1/consent-link/${action}`, {
    method: "POST",
    headers: { "Consent-Link": token, "Idempotency-Key": idempotencyKey },
  });
}

// Why an approval was blocked: the re-check's primary Reason code (ADR-0010).
const BLOCKED_BECAUSE: Record<string, string> = {
  MANDATE_REVOKED: "The mandate for this agent was revoked.",
  MANDATE_EXPIRED: "The mandate for this agent has expired.",
  MANDATE_NOT_YET_VALID: "The mandate for this agent isn't active yet.",
  AGENT_DISABLED: "This agent has been switched off.",
  AGENT_NOT_ALLOWED: "This agent isn't allowed to pay under the mandate.",
  MONTHLY_CAP_EXCEEDED: "This payment would take you over your monthly limit.",
  PER_TXN_CAP_EXCEEDED: "This payment is over your limit for a single payment.",
  MCC_NOT_ALLOWED: "The mandate doesn't allow this kind of merchant.",
  MCC_HIGH_RISK: "Payments to this kind of merchant are never allowed.",
  CURRENCY_MISMATCH: "This payment is in a different currency from the mandate.",
};

export function blockedReason(codes: string[] | null): string {
  const primary = codes?.[0];
  return (primary && BLOCKED_BECAUSE[primary]) ?? "Your mandate no longer allows this payment.";
}

export function timeLeft(expiresAt: string, now: number): string {
  const seconds = Math.max(0, Math.floor((Date.parse(expiresAt) - now) / 1000));
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}
