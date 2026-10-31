"use client";

import { useEffect, useRef, useState } from "react";

import {
  blockedReason,
  resolveConsent,
  timeLeft,
  viewConsent,
  type ConsentView,
  type Resolution,
} from "@/lib/consent-api";

type State =
  | { kind: "loading" }
  | { kind: "unusable"; message: string }
  | { kind: "deciding"; view: ConsentView; sending: "approve" | "decline" | null }
  | { kind: "done"; view: ConsentView; resolution: Resolution };

const NO_LONGER_WORKS =
  "This link no longer works. It may have expired, been used already, or been replaced by a newer one. Ask for a new link if you still need to decide.";
const TRY_AGAIN = "We couldn't reach AgentPay Guard. Check your connection and reload the page.";

const VERDICT_TEXT = {
  match: "The cart matches what you asked the agent to buy.",
  mismatch: "The cart doesn't match what you asked the agent to buy.",
  unknown: "We couldn't confirm that the cart matches what you asked for.",
} as const;

export function ConsentScreen() {
  const [state, setState] = useState<State>({ kind: "loading" });
  // The clock starts after the first render: the page prerenders without one.
  const [now, setNow] = useState<number | null>(null);
  const token = useRef("");
  // One Idempotency-Key per decision: a double tap or a retry resolves once.
  const keys = useRef<{ approve?: string; decline?: string }>({});

  useEffect(() => {
    token.current = window.location.hash.slice(1);
    if (!token.current) {
      setState({ kind: "unusable", message: NO_LONGER_WORKS });
      return;
    }
    viewConsent(token.current).then((result) => {
      if (result.ok) setState({ kind: "deciding", view: result.data, sending: null });
      else setState({ kind: "unusable", message: result.invalid ? NO_LONGER_WORKS : TRY_AGAIN });
    });
  }, []);

  useEffect(() => {
    const tick = () => setNow(Date.now());
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, []);

  async function decide(action: "approve" | "decline") {
    if (state.kind !== "deciding" || state.sending) return;
    const key = (keys.current[action] ??= crypto.randomUUID());
    setState({ ...state, sending: action });
    const result = await resolveConsent(token.current, action, key);
    if (result.ok) setState({ kind: "done", view: state.view, resolution: result.data });
    else if (result.invalid) setState({ kind: "unusable", message: NO_LONGER_WORKS });
    else setState({ ...state, sending: null });
  }

  if (state.kind === "loading") return <Frame><p className="text-neutral-500">Loading…</p></Frame>;
  if (state.kind === "unusable") return <Frame><p>{state.message}</p></Frame>;

  const { view } = state;
  const expired = now !== null && Date.parse(view.expires_at) <= now;
  if (state.kind === "deciding" && expired) return <Frame><p>{NO_LONGER_WORKS}</p></Frame>;

  return (
    <Frame>
      <Purchase view={view} />
      {state.kind === "done" ? (
        <Outcome resolution={state.resolution} />
      ) : (
        <>
          {view.mandate_inactive && (
            <p className="rounded-md border border-amber-500 bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100">
              Your mandate for this agent has stopped, or the agent was switched off. If you
              approve, the payment will still be blocked.
            </p>
          )}
          <p className="text-sm text-neutral-500">
            Time left to decide: <span className="font-mono">{now === null ? "…" : timeLeft(view.expires_at, now)}</span>
          </p>
          <div className="flex gap-3">
            <button
              type="button"
              disabled={state.sending !== null}
              onClick={() => decide("approve")}
              className="flex-1 rounded-md bg-emerald-600 px-4 py-3 font-medium text-white disabled:opacity-50"
            >
              {state.sending === "approve" ? "Approving…" : "Approve"}
            </button>
            <button
              type="button"
              disabled={state.sending !== null}
              onClick={() => decide("decline")}
              className="flex-1 rounded-md border border-neutral-400 px-4 py-3 font-medium disabled:opacity-50"
            >
              {state.sending === "decline" ? "Declining…" : "Decline"}
            </button>
          </div>
        </>
      )}
    </Frame>
  );
}

function Frame({ children }: { children: React.ReactNode }) {
  return (
    <main className="mx-auto flex w-full max-w-md flex-1 flex-col gap-5 px-4 py-12">
      <h1 className="text-xl font-semibold">Approve a payment</h1>
      {children}
    </main>
  );
}

// Facts AgentPay Guard worked out itself. Never the agent's own explanation: a
// manipulated agent could use it to talk you into approving.
function Purchase({ view }: { view: ConsentView }) {
  const { payment } = view;
  return (
    <section className="flex flex-col gap-4">
      <div className="rounded-md border border-neutral-300 p-4 dark:border-neutral-700">
        <p className="text-3xl font-semibold">{payment.amount_text}</p>
        <p className="mt-1">
          to <span className="font-medium">{payment.merchant_name}</span>
          <span className="text-neutral-500"> · category {payment.mcc}</span>
        </p>
        {payment.agent_name && (
          <p className="mt-1 text-sm text-neutral-500">Asked for by {payment.agent_name}</p>
        )}
      </div>
      <div>
        <h2 className="font-medium">Why we&apos;re asking</h2>
        <ul className="mt-2 list-disc space-y-1 pl-5">
          {view.reasons.map((reason) => (
            <li key={reason.code}>{reason.text}</li>
          ))}
        </ul>
        {view.intent_verdict && <p className="mt-2">{VERDICT_TEXT[view.intent_verdict]}</p>}
      </div>
    </section>
  );
}

function Outcome({ resolution }: { resolution: Resolution }) {
  if (resolution.status === "approved") {
    return <p className="rounded-md bg-emerald-50 p-3 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100">Approved: the payment is now being scheduled.</p>;
  }
  if (resolution.status === "declined") {
    return <p className="rounded-md bg-neutral-100 p-3 dark:bg-neutral-900">Declined. The agent won&apos;t make this payment.</p>;
  }
  return (
    <p className="rounded-md bg-red-50 p-3 text-red-900 dark:bg-red-950 dark:text-red-100">
      Not approved. {blockedReason(resolution.reason_codes)} The agent won&apos;t make this
      payment.
    </p>
  );
}
