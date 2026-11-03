"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  FACT_TEXT,
  REASON_TEXT,
  REFUSAL_TEXT,
  regenerate,
  signOff,
  viewReview,
  type Assessment,
  type BundleRecord,
  type Claim,
  type Dispute,
  type Pack,
  type ReviewView,
} from "@/lib/review-api";

type State =
  | { kind: "loading" }
  | { kind: "unusable"; message: string }
  | { kind: "reviewing"; view: ReviewView; sending: "sign-off" | "regenerate" | null; problem: string | null };

const NO_LONGER_WORKS =
  "This review link no longer works. It may have expired, been used to sign off, been replaced by a newer one, or the dispute may be closed. Ask for a new link if you still need to review it.";
const TRY_AGAIN = "We couldn't reach AgentPay Guard. Check your connection and try again.";
// While a pack is being built, look again this often.
const POLL_MS = 3000;

export function DisputeViewer() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const token = useRef("");
  // One Idempotency-Key per action and pack version: a double click acts once.
  const keys = useRef<Record<string, string>>({});

  const load = useCallback(async () => {
    const result = await viewReview(token.current);
    if (result.ok) {
      setState((current) => ({
        kind: "reviewing",
        view: result.data,
        sending: null,
        problem: current.kind === "reviewing" ? current.problem : null,
      }));
    } else {
      setState({ kind: "unusable", message: result.invalid ? NO_LONGER_WORKS : TRY_AGAIN });
    }
  }, []);

  useEffect(() => {
    token.current = window.location.hash.slice(1);
    if (!token.current) {
      setState({ kind: "unusable", message: NO_LONGER_WORKS });
      return;
    }
    void load();
  }, [load]);

  // A pack being built or rebuilt: check back until the new version is in review.
  const building = state.kind === "reviewing" && state.view.dispute.status === "open";
  useEffect(() => {
    if (!building) return;
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [building, load]);

  async function act(action: "sign-off" | "regenerate") {
    if (state.kind !== "reviewing" || state.sending) return;
    const pack = state.view.dispute.pack;
    if (!pack) return;
    const slot = `${action}:${pack.version}`;
    const key = (keys.current[slot] ??= crypto.randomUUID());
    setState({ ...state, sending: action, problem: null });
    const result =
      action === "sign-off"
        ? await signOff(token.current, pack.version, key)
        : await regenerate(token.current, key);
    if (result.ok) {
      setState({
        kind: "reviewing",
        view: { ...state.view, dispute: result.data },
        sending: null,
        problem: null,
      });
    } else if (result.invalid) {
      setState({ kind: "unusable", message: NO_LONGER_WORKS });
    } else {
      const problem = (result.code && REFUSAL_TEXT[result.code]) ?? TRY_AGAIN;
      setState({ ...state, sending: null, problem });
    }
  }

  if (state.kind === "loading") return <Frame><p className="text-neutral-500">Loading…</p></Frame>;
  if (state.kind === "unusable") return <Frame><p>{state.message}</p></Frame>;

  const { dispute, reviewer } = state.view;
  const pack = dispute.pack;
  return (
    <Frame>
      <Header dispute={dispute} reviewerName={reviewer.name} />
      {dispute.status === "open" && (
        <p className="rounded-md border border-neutral-300 p-3 text-sm print:hidden dark:border-neutral-700">
          {pack
            ? `Building version ${pack.version + 1} of the evidence pack. This page updates when it's ready.`
            : "Building the evidence pack. This page updates when it's ready."}
        </p>
      )}
      {pack && <PackView pack={pack} />}
      {dispute.status === "ready" && dispute.sign_off && (
        <p className="rounded-md bg-emerald-50 p-3 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100">
          Signed off by {dispute.sign_off.reviewer.name} on {when(dispute.sign_off.signed_off_at)}{" "}
          (version {dispute.sign_off.pack_version}, pack hash{" "}
          <span className="font-mono text-xs break-all">{dispute.sign_off.pack_hash}</span>). Submit
          it to your acquirer before {when(dispute.respond_by)}.
        </p>
      )}
      {state.problem && (
        <p className="rounded-md bg-red-50 p-3 text-red-900 print:hidden dark:bg-red-950 dark:text-red-100">
          {state.problem}
        </p>
      )}
      {dispute.status === "in_review" && pack && (
        <div className="flex gap-3 print:hidden">
          <button
            type="button"
            disabled={state.sending !== null}
            onClick={() => act("sign-off")}
            className="flex-1 rounded-md bg-emerald-600 px-4 py-3 font-medium text-white disabled:opacity-50"
          >
            {state.sending === "sign-off" ? "Signing off…" : `Sign off version ${pack.version}`}
          </button>
          <button
            type="button"
            disabled={state.sending !== null}
            onClick={() => act("regenerate")}
            className="flex-1 rounded-md border border-neutral-400 px-4 py-3 font-medium disabled:opacity-50"
          >
            {state.sending === "regenerate" ? "Asking…" : "Regenerate"}
          </button>
        </div>
      )}
      {pack && (
        <button
          type="button"
          onClick={() => window.print()}
          className="self-start text-sm text-neutral-500 underline print:hidden"
        >
          Print or save as PDF
        </button>
      )}
    </Frame>
  );
}

function Frame({ children }: { children: React.ReactNode }) {
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-6 px-4 py-12 print:max-w-none print:py-0">
      <h1 className="text-xl font-semibold">Dispute evidence pack</h1>
      {children}
    </main>
  );
}

function Header({ dispute, reviewerName }: { dispute: Dispute; reviewerName: string }) {
  return (
    <section className="grid gap-1 text-sm sm:grid-cols-2">
      <p>
        <span className="text-neutral-500">Reason: </span>
        {REASON_TEXT[dispute.reason] ?? dispute.reason}
      </p>
      <p>
        <span className="text-neutral-500">Respond by: </span>
        {when(dispute.respond_by)}
      </p>
      <p>
        <span className="text-neutral-500">Payment: </span>
        <span className="font-mono">{dispute.payment_id}</span>
      </p>
      <p>
        <span className="text-neutral-500">Reviewer: </span>
        {reviewerName}
      </p>
    </section>
  );
}

function PackView({ pack }: { pack: Pack }) {
  const records = new Map<string, BundleRecord>(
    [...pack.bundle.trail, ...pack.bundle.related, ...(pack.bundle.payer_payments ?? [])].map(
      (r) => [r.audit_record_id, r],
    ),
  );
  const cited = [...new Set(pack.narrative.flatMap((c) => c.cites))]
    .map((id) => records.get(id))
    .filter((r): r is BundleRecord => r !== undefined);
  return (
    <>
      {pack.assessment ? (
        <AssessmentView assessment={pack.assessment} />
      ) : (
        <p className="text-sm text-neutral-500">
          The Policy pack this was built with has no dispute rules, so there are no network
          codes or evidence strength.
        </p>
      )}
      <section className="flex flex-col gap-3">
        <h2 className="font-medium">What happened</h2>
        {pack.narrative_source === "template" && pack.narrative_note && (
          <p className="rounded-md border border-amber-500 bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100">
            Written by code from the evidence: {pack.narrative_note}.
          </p>
        )}
        <ol className="flex flex-col gap-3">
          {pack.narrative.map((claim, i) => (
            <ClaimView key={i} claim={claim} records={records} />
          ))}
        </ol>
      </section>
      <section className="hidden print:block">
        <h2 className="font-medium">Cited Audit records</h2>
        {cited.map((r) => (
          <RecordView key={r.audit_record_id} record={r} />
        ))}
      </section>
      <p className="text-xs text-neutral-500 break-all">
        Version {pack.version}, built {when(pack.built_at)}
        {pack.policy_pack && ` with Policy pack ${pack.policy_pack.id}@${pack.policy_pack.version}`}.
        Bundle hash <span className="font-mono">{pack.bundle_hash}</span>.
      </p>
    </>
  );
}

const STRENGTH_STYLE = {
  strong: "bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100",
  mixed: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-100",
  weak: "bg-red-100 text-red-900 dark:bg-red-950 dark:text-red-100",
} as const;

function AssessmentView({ assessment }: { assessment: Assessment }) {
  return (
    <section className="flex flex-col gap-3 rounded-md border border-neutral-300 p-4 dark:border-neutral-700">
      <div className="flex flex-wrap items-center gap-3">
        <span className={`rounded px-2 py-1 text-sm font-semibold ${STRENGTH_STYLE[assessment.strength]}`}>
          Evidence {assessment.strength}
        </span>
        <span className="text-sm">
          Visa <span className="font-mono">{assessment.visa_code}</span> · Mastercard{" "}
          <span className="font-mono">{assessment.mastercard_code}</span>
        </span>
      </div>
      <div className="grid gap-3 text-sm sm:grid-cols-2">
        <div>
          <h3 className="font-medium">What the evidence shows</h3>
          <ul className="mt-1 list-disc space-y-1 pl-5">
            {assessment.facts.map((f) => (
              <li key={f}>{FACT_TEXT[f] ?? f}</li>
            ))}
          </ul>
        </div>
        <div>
          <h3 className="font-medium">Missing for strong evidence</h3>
          {assessment.missing.length ? (
            <ul className="mt-1 list-disc space-y-1 pl-5">
              {assessment.missing.map((f) => (
                <li key={f}>{FACT_TEXT[f] ?? f}</li>
              ))}
            </ul>
          ) : (
            <p className="mt-1">Nothing.</p>
          )}
        </div>
      </div>
      {assessment.notes.map((note) => (
        <p key={note} className="text-sm text-neutral-600 dark:text-neutral-400">
          {note}
        </p>
      ))}
    </section>
  );
}

function ClaimView({ claim, records }: { claim: Claim; records: Map<string, BundleRecord> }) {
  return (
    <li className="flex flex-col gap-1">
      <p>{claim.text}</p>
      <div className="flex flex-wrap gap-2 print:hidden">
        {claim.cites.map((id) => {
          const record = records.get(id);
          return (
            <details key={id} className="text-xs">
              <summary className="cursor-pointer font-mono text-neutral-500">
                {id}
                {record && ` · ${record.kind} #${record.seq}`}
              </summary>
              {record ? <RecordView record={record} /> : <p>Not in the bundle.</p>}
            </details>
          );
        })}
      </div>
      <p className="hidden font-mono text-xs text-neutral-500 print:block">
        Cites {claim.cites.join(", ")}
      </p>
    </li>
  );
}

function RecordView({ record }: { record: BundleRecord }) {
  // Show the record's own sections; the chain fields are summarised above it.
  const shown = Object.fromEntries(
    Object.entries(record.record).filter(
      ([k]) => !["audit_record_id", "tenant_id", "seq", "prev_hash", "kind"].includes(k),
    ),
  );
  return (
    <div className="mt-2 rounded border border-neutral-200 p-2 text-xs dark:border-neutral-800 print:break-inside-avoid">
      <p className="font-mono">
        {record.audit_record_id} · {record.kind} · seq {record.seq} · {when(record.record.recorded_at)}
      </p>
      <p className="font-mono break-all text-neutral-500">{record.record_hash}</p>
      <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-all">
        {JSON.stringify(shown, null, 2)}
      </pre>
    </div>
  );
}

// Every time in UTC, as the Audit records hold it. (`dateStyle` can't be combined with
// `timeZoneName`, so the zone is written out.)
function when(iso: string): string {
  const text = new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  });
  return `${text} UTC`;
}
