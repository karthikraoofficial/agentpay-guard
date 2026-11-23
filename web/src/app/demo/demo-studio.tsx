"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { OUTCOME_TEXT, reasonText } from "@/lib/reason-codes";
import { nextStep, startRun, type Card, type ConsentStatus, type Failure } from "@/lib/runner-api";
import { GROUPS, SCENARIOS, type Scenario } from "@/lib/scenarios";

const STEP_PAUSE_MS = 1200; // between turns while playing
const POLL_MS = 2000; // while a step waits on the worker or a person
const MAX_POLLS = 120; // about four minutes, for the worker
const HUMAN_POLLS = 330; // about eleven minutes: past a Consent request's 10-minute TTL

type Pane = "chat" | "phone" | "inspector";

type Run = {
  scenario: Scenario;
  runId: string | null;
  steps: number;
  cards: Card[];
  next: number;
  busy: boolean;
  waiting: Card | null;
  playing: boolean;
  failure: Failure | null;
};

const ACTOR = {
  agent: "ShopBot",
  tenant: "Wallet app",
  payer: "Asha",
  reviewer: "Reviewer",
  visitor: "Fast-forward",
} as const;

const sleep = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

function failureText(failure: Failure): string {
  switch (failure.kind) {
    case "busy":
      return `The demo is busy. Try again in ${failure.seconds} seconds.`;
    case "gone":
      return "This run has ended (it expired, or the demo restarted). Start it again.";
    case "failed":
      return `A step failed: ${failure.detail}`;
    case "unreachable":
      return "We couldn't reach the demo. Check your connection and try again.";
  }
}

export function DemoStudio() {
  const [run, setRun] = useState<Run | null>(null);
  const [pane, setPane] = useState<Pane>("chat");
  // Bumped when the visitor leaves a run, so its loops stop.
  const generation = useRef(0);
  // The run as the async loops see it; every change goes through `update` or `choose`.
  const current = useRef<Run | null>(null);

  useEffect(() => () => void (generation.current += 1), []);

  function update(gen: number, change: Partial<Run>) {
    if (gen !== generation.current) return;
    setRun((r) => (r ? { ...r, ...change } : r));
    if (current.current) current.current = { ...current.current, ...change };
  }

  function choose(scenario: Scenario | null) {
    generation.current += 1;
    setPane("chat");
    const fresh: Run | null =
      scenario && {
        scenario,
        runId: null,
        steps: scenario.steps.length + 1,
        cards: [],
        next: 0,
        busy: false,
        waiting: null,
        playing: false,
        failure: null,
      };
    current.current = fresh;
    setRun(fresh);
  }

  // One turn: start the run if needed, then run the next step, polling while it waits.
  // Returns false when the run can't go on (finished, failed or left).
  async function turn(gen: number, press = false): Promise<boolean> {
    const r = current.current;
    if (!r || r.busy || gen !== generation.current) return false;
    if (r.cards.at(-1)?.finished) return false;
    update(gen, { busy: true, failure: null });
    let runId = r.runId;
    if (runId === null) {
      const started = await startRun(r.scenario.id);
      if (!started.ok) {
        update(gen, { busy: false, failure: started.failure, playing: false });
        return false;
      }
      runId = started.data.run_id;
      update(gen, { runId, steps: started.data.steps });
    }
    for (let polls = 0; ; polls += 1) {
      // Only the first ask carries the press; a retry of this loop just reads.
      const answer = await nextStep(runId, r.next, press && polls === 0);
      if (gen !== generation.current) return false;
      if (!answer.ok) {
        update(gen, { busy: false, waiting: null, failure: answer.failure, playing: false });
        return false;
      }
      const card = answer.data;
      if (!card.waiting) {
        // Asha has answered: at phone width, go back to the chat, which carries on.
        if (card.actor === "payer" && window.matchMedia("(max-width: 767px)").matches) setPane("chat");
        const cards = [...(current.current?.cards ?? []), card];
        update(gen, { cards, next: r.next + 1, busy: false, waiting: null });
        if (card.finished) update(gen, { playing: false });
        return !card.finished;
      }
      if (card.fast_forward) {
        // The visitor's turn: Play stops here until they press the button.
        update(gen, { busy: false, waiting: card, playing: false });
        return false;
      }
      update(gen, { waiting: card });
      // Asha answers on her phone: at phone width, show it.
      if (card.link && window.matchMedia("(max-width: 767px)").matches) setPane("phone");
      if (polls >= (card.actor === "payer" ? HUMAN_POLLS : MAX_POLLS)) break;
      await sleep(POLL_MS);
      if (gen !== generation.current) return false;
    }
    update(gen, {
      busy: false,
      waiting: null,
      playing: false,
      failure: { kind: "failed", detail: "still waiting after a few minutes" },
    });
    return false;
  }

  async function play() {
    const gen = generation.current;
    update(gen, { playing: true });
    while (current.current?.playing && gen === generation.current) {
      if (!(await turn(gen))) break;
      await sleep(STEP_PAUSE_MS);
    }
    update(gen, { playing: false });
  }

  if (run === null) return <Picker onChoose={choose} />;

  const finished = run.cards.at(-1)?.finished ?? false;
  const decision = [...run.cards].reverse().find((c) => c.decision)?.decision ?? null;
  // The latest Payer step: still waiting on Asha, or answered.
  const asking = [...run.cards, ...(run.waiting ? [run.waiting] : [])].reverse().find((c) => c.link) ?? null;

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-1 flex-col gap-4 px-4 py-6">
      <header className="flex flex-col gap-2">
        <button type="button" onClick={() => choose(null)} className="self-start text-sm underline">
          ← All journeys
        </button>
        <h1 className="text-xl font-semibold">
          {run.scenario.id}. {run.scenario.title}
        </h1>
        <p className="max-w-3xl text-sm text-neutral-700 dark:text-neutral-300">{run.scenario.story}</p>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={run.busy || run.playing || finished}
            onClick={() => void turn(generation.current)}
            className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm disabled:opacity-40 dark:border-neutral-700"
          >
            {run.cards.length === 0 ? "Start: first turn" : "Next turn"}
          </button>
          {run.playing ? (
            <button
              type="button"
              onClick={() => update(generation.current, { playing: false })}
              className="rounded-md bg-neutral-900 px-3 py-1.5 text-sm text-white dark:bg-neutral-100 dark:text-neutral-900"
            >
              Pause
            </button>
          ) : (
            <button
              type="button"
              disabled={run.busy || finished}
              onClick={() => void play()}
              className="rounded-md bg-neutral-900 px-3 py-1.5 text-sm text-white disabled:opacity-40 dark:bg-neutral-100 dark:text-neutral-900"
            >
              Play
            </button>
          )}
          <span className="text-xs text-neutral-500 tabular-nums">
            Turn {Math.min(run.cards.length, run.steps)} of {run.steps}
          </span>
        </div>
        {run.failure && (
          <p role="alert" className="rounded-md border border-amber-300 bg-amber-50 p-2 text-sm dark:border-amber-800 dark:bg-amber-950">
            {failureText(run.failure)}
          </p>
        )}
      </header>

      <nav className="grid grid-cols-3 gap-1 md:hidden" aria-label="Panes">
        {(["chat", "phone", "inspector"] as const).map((p) => (
          <button
            key={p}
            type="button"
            onClick={() => setPane(p)}
            aria-pressed={pane === p}
            className={`rounded-md border px-2 py-1.5 text-sm capitalize ${
              pane === p
                ? "border-neutral-900 bg-neutral-900 text-white dark:border-neutral-100 dark:bg-neutral-100 dark:text-neutral-900"
                : "border-neutral-300 dark:border-neutral-700"
            }`}
          >
            {p === "inspector" ? "Guard inspector" : p}
          </button>
        ))}
      </nav>

      <div className="grid min-w-0 gap-4 md:grid-cols-[minmax(0,1.5fr)_minmax(0,0.8fr)_minmax(0,1fr)]">
        <section className={`${pane === "chat" ? "" : "hidden"} min-w-0 md:block`} aria-label="Chat">
          <h2 className="mb-2 text-sm font-semibold text-neutral-500">Chat: Asha and ShopBot</h2>
          <Chat
            cards={run.cards}
            waiting={run.waiting}
            busy={run.busy}
            onPress={() => void turn(generation.current, true)}
          />
          {finished && <Ending scenario={run.scenario} card={run.cards.at(-1)!} />}
        </section>
        <section className={`${pane === "phone" ? "" : "hidden"} min-w-0 md:block`} aria-label="Phone">
          <h2 className="mb-2 text-sm font-semibold text-neutral-500">Asha&apos;s phone</h2>
          <Phone card={asking} />
        </section>
        <section className={`${pane === "inspector" ? "" : "hidden"} min-w-0 md:block`} aria-label="Guard inspector">
          <h2 className="mb-2 text-sm font-semibold text-neutral-500">Guard inspector</h2>
          <Inspector decision={decision} />
          {finished && (
            <p className="mt-3 rounded-md border border-sky-300 bg-sky-50 p-3 text-sm dark:border-sky-800 dark:bg-sky-950">
              <span className="font-semibold">What to notice: </span>
              {run.scenario.what_to_notice}
            </p>
          )}
        </section>
      </div>
    </main>
  );
}

function Picker({ onChoose }: { onChoose: (s: Scenario) => void }) {
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-6 px-4 py-10">
      <header className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold">AgentPay Guard demo</h1>
        <p className="text-neutral-700 dark:text-neutral-300">
          Asha uses ShopBot, an AI shopping assistant. Before ShopBot pays, it asks AgentPay Guard.
          Pick a journey and watch it play: the conversation is scripted, but every call to the
          Guard is real, and so is every Decision and Audit record. No real money moves.
        </p>
        <p className="text-sm">
          <Link href="/how-it-works" className="underline">
            How it works
          </Link>{" "}
          ·{" "}
          <Link href="/ops" className="underline">
            Ops console
          </Link>
        </p>
      </header>
      {SCENARIOS.length === 0 && <p>No journeys in this build.</p>}
      {GROUPS.map((group) => {
        const inGroup = SCENARIOS.filter((s) => s.group === group.id);
        return (
          <section key={group.id} className="flex flex-col gap-2">
            <h2 className="font-semibold">
              {group.id}. {group.name}
            </h2>
            {inGroup.length === 0 ? (
              <p className="text-sm text-neutral-500">Coming soon.</p>
            ) : (
              <ul className="flex flex-col gap-2">
                {inGroup.map((s) => (
                  <li key={s.id}>
                    <button
                      type="button"
                      onClick={() => onChoose(s)}
                      className="w-full rounded-md border border-neutral-300 p-3 text-left hover:border-neutral-500 dark:border-neutral-700"
                    >
                      <span className="font-medium">
                        {s.id}. {s.title}
                      </span>
                      <span className="mt-1 block text-sm text-neutral-600 dark:text-neutral-400">{s.story}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        );
      })}
    </main>
  );
}

function Chat({
  cards,
  waiting,
  busy,
  onPress,
}: {
  cards: Card[];
  waiting: Card | null;
  busy: boolean;
  onPress: () => void;
}) {
  if (cards.length === 0 && !waiting) {
    return <p className="text-sm text-neutral-500">Press Play, or take it one turn at a time.</p>;
  }
  return (
    <ol className="flex flex-col gap-3">
      {cards.map((card) => (
        <li key={card.step} className="flex flex-col gap-2">
          {card.chat.map((line, i) => (
            <p
              key={i}
              className={`max-w-[85%] rounded-2xl px-3 py-2 text-sm ${
                line.speaker === "asha"
                  ? "self-end bg-sky-100 dark:bg-sky-900"
                  : "self-start bg-neutral-100 dark:bg-neutral-800"
              }`}
            >
              <span className="block text-xs font-semibold">{line.speaker === "asha" ? "Asha" : "ShopBot"}</span>
              {line.text}
            </p>
          ))}
          {card.call && <CallCard card={card} />}
          {card.refused && <Refused code={card.refused} />}
          {card.decision && <Verdict decision={card.decision} />}
          {card.actor === "payer" && card.consent && <Answer consent={card.consent} />}
        </li>
      ))}
      {waiting?.fast_forward && (
        <li className="flex flex-col gap-2">
          {waiting.chat.map((line, i) => (
            <p key={i} className="max-w-[85%] self-start rounded-2xl bg-neutral-100 px-3 py-2 text-sm dark:bg-neutral-800">
              <span className="block text-xs font-semibold">{line.speaker === "asha" ? "Asha" : "ShopBot"}</span>
              {line.text}
            </p>
          ))}
          <button
            type="button"
            disabled={busy}
            onClick={onPress}
            className="self-center rounded-full bg-sky-700 px-4 py-2 text-sm font-medium text-white hover:bg-sky-800 disabled:opacity-50"
          >
            ⏩ {waiting.fast_forward}
          </button>
          <p className="text-center text-xs text-neutral-500">
            A Demo control ends this timer now. No clock moves and no record is edited.
          </p>
        </li>
      )}
      {waiting && !waiting.fast_forward && (
        <li className="text-sm text-neutral-500" aria-live="polite">
          {waiting.actor === "payer"
            ? "Waiting for Asha to approve or decline on her phone…"
            : waiting.call?.name === "get_payment"
              ? "Waiting for the bank to take the money…"
              : "Waiting…"}
        </li>
      )}
    </ol>
  );
}

function CallCard({ card }: { card: Card }) {
  const call = card.call!;
  return (
    <div className="min-w-0 rounded-md border border-neutral-300 p-2 text-sm dark:border-neutral-700">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="text-xs text-neutral-500">
          {card.actor === "payer" && call.name.startsWith("GET ") ? "Wallet app checked" : `${ACTOR[card.actor]} called`}
        </span>
        <code className="font-mono text-xs font-semibold break-all">{call.name}</code>
        <span className="text-xs text-neutral-500">{call.kind === "tool" ? "(MCP tool)" : "(HTTP route)"}</span>
      </div>
      <details className="mt-1">
        <summary className="cursor-pointer text-xs text-neutral-600 dark:text-neutral-400">Request and response</summary>
        <JsonBlock label="Request" value={call.request} />
        <JsonBlock label="Response" value={call.response} />
      </details>
    </div>
  );
}

function JsonBlock({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="mt-1">
      <div className="text-xs font-semibold">{label}</div>
      <pre className="max-h-64 overflow-auto rounded bg-neutral-50 p-2 text-xs dark:bg-neutral-900">
        {value === null ? "(none)" : JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}

const OUTCOME_TONE = {
  allow: "border-emerald-300 bg-emerald-50 dark:border-emerald-800 dark:bg-emerald-950",
  step_up: "border-amber-300 bg-amber-50 dark:border-amber-800 dark:bg-amber-950",
  block: "border-red-300 bg-red-50 dark:border-red-800 dark:bg-red-950",
} as const;

function Verdict({ decision }: { decision: NonNullable<Card["decision"]> }) {
  return (
    <div className={`rounded-md border p-2 text-sm ${OUTCOME_TONE[decision.outcome]}`}>
      <div className="font-semibold">{OUTCOME_TEXT[decision.outcome] ?? decision.outcome}</div>
      {decision.reason_codes.length === 0 ? (
        <p className="text-xs">No Reason codes: nothing about this payment needed a second look.</p>
      ) : (
        <ul className="mt-1 flex flex-col gap-1">
          {decision.reason_codes.map((code) => (
            <li key={code} className="text-xs">
              <code className="font-mono font-semibold">{code}</code>: {reasonText(code)}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

const ANSWER_TEXT: Record<ConsentStatus, string> = {
  pending: "Asha hasn't answered yet.",
  approved: "Asha approved. ShopBot may now pay.",
  declined: "Asha declined. No payment can be made on this request.",
  expired: "Asha didn't answer in time, so the request expired. Silence never means yes.",
  blocked: "Asha approved, but the Guard checked again and blocked it:",
};

const ANSWER_TONE: Record<ConsentStatus, string> = {
  pending: "border-neutral-300 dark:border-neutral-700",
  approved: "border-emerald-300 bg-emerald-50 dark:border-emerald-800 dark:bg-emerald-950",
  declined: "border-amber-300 bg-amber-50 dark:border-amber-800 dark:bg-amber-950",
  expired: "border-amber-300 bg-amber-50 dark:border-amber-800 dark:bg-amber-950",
  blocked: "border-red-300 bg-red-50 dark:border-red-800 dark:bg-red-950",
};

function Answer({ consent }: { consent: NonNullable<Card["consent"]> }) {
  return (
    <div className={`rounded-md border p-2 text-sm ${ANSWER_TONE[consent.status]}`}>
      <div className="font-semibold">{ANSWER_TEXT[consent.status]}</div>
      {consent.reason_codes?.[0] && (
        // The re-check's primary Reason code says why (ADR-0010).
        <p className="mt-1 text-xs">
          <code className="font-mono font-semibold">{consent.reason_codes[0]}</code>:{" "}
          {reasonText(consent.reason_codes[0])}
        </p>
      )}
    </div>
  );
}

function Refused({ code }: { code: string }) {
  return (
    <div className={`rounded-md border p-2 text-sm ${OUTCOME_TONE.block}`}>
      <span className="font-semibold">Refused: </span>
      <code className="font-mono">{code}</code>
      {code === "INSUFFICIENT_SCOPE" && (
        <p className="text-xs">ShopBot&apos;s key can ask for Decisions, never approve them.</p>
      )}
    </div>
  );
}

// Asha's phone. While she must answer, it is the real consent screen on the run's Consent
// link (the token is in the #fragment, ADR-0008); afterwards, her answer.
function Phone({ card }: { card: Card | null }) {
  const asking = card?.waiting && card.link;
  return (
    <div className="mx-auto flex aspect-[9/16] w-full max-w-[300px] flex-col overflow-hidden rounded-[2rem] border-4 border-neutral-800 p-2 dark:border-neutral-300">
      <div className="mx-auto mb-2 h-1.5 w-16 shrink-0 rounded-full bg-neutral-300 dark:bg-neutral-700" />
      {asking ? (
        <iframe
          key={card.link}
          src={card.link!}
          title="Asha's consent screen"
          className="min-h-0 w-full flex-1 rounded-xl border-0 bg-white"
        />
      ) : card?.consent ? (
        <div className="m-auto flex flex-col gap-2 p-2 text-center text-sm">
          <Answer consent={card.consent} />
        </div>
      ) : (
        <p className="m-auto text-center text-sm text-neutral-500">Nothing for Asha to approve yet.</p>
      )}
    </div>
  );
}

function Inspector({ decision }: { decision: Card["decision"] }) {
  if (!decision) return <p className="text-sm text-neutral-500">No Decision yet.</p>;
  const rows: [string, ReactNode][] = [
    ["Outcome", <code key="o" className="font-mono">{decision.outcome}</code>],
    [
      "Reason codes",
      decision.reason_codes.length === 0 ? "none" : (
        <span key="r" className="flex flex-col gap-1">
          {decision.reason_codes.map((code) => (
            <span key={code}>
              <code className="font-mono">{code}</code>
              <span className="block text-xs text-neutral-600 dark:text-neutral-400">{reasonText(code)}</span>
            </span>
          ))}
        </span>
      ),
    ],
    ["Policy pack", <code key="p" className="font-mono">{decision.policy_pack}</code>],
    ["Pack hash", <code key="h" className="font-mono break-all">{decision.pack_hash}</code>],
    ["Decision", <code key="d" className="font-mono break-all">{decision.decision_id}</code>],
    ["Audit record", <code key="a" className="font-mono break-all">{decision.audit_record_id}</code>],
  ];
  return (
    <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-2 rounded-md border border-neutral-300 p-3 text-sm dark:border-neutral-700">
      {rows.map(([name, value]) => (
        <div key={name} className="contents">
          <dt className="text-neutral-500">{name}</dt>
          <dd className="min-w-0">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function Ending({ scenario, card }: { scenario: Scenario; card: Card }) {
  const ok = card.mismatches.length === 0;
  return (
    <div
      className={`mt-3 rounded-md border p-3 text-sm ${
        ok
          ? "border-emerald-300 bg-emerald-50 dark:border-emerald-800 dark:bg-emerald-950"
          : "border-red-300 bg-red-50 dark:border-red-800 dark:bg-red-950"
      }`}
    >
      {ok ? (
        <p>The journey ended as expected.</p>
      ) : (
        <>
          <p className="font-semibold">This run didn&apos;t end as the script expects:</p>
          <ul className="list-disc pl-5">
            {card.mismatches.map((m) => (
              <li key={m}>{m}</li>
            ))}
          </ul>
        </>
      )}
      <p className="mt-2 md:hidden">
        <span className="font-semibold">What to notice: </span>
        {scenario.what_to_notice}
      </p>
    </div>
  );
}
