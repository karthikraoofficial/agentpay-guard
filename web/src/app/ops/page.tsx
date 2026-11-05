import type { Metadata } from "next";
import Link from "next/link";

import { evalReport, reportSource, type FileEntry } from "@/lib/eval-report";

export const metadata: Metadata = {
  title: "Ops console · AgentPay Guard",
  description: "The eval scorecard: how the policy engine scores on every labelled case.",
};

const NONE = "–";

function count(value: number | null): string {
  return value === null ? NONE : String(value);
}

function percent(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

function label(scenario: string): string {
  const words = scenario.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function Tile({ name, value, good }: { name: string; value: string; good?: boolean }) {
  const tone =
    good === undefined
      ? "border-neutral-200 dark:border-neutral-800"
      : good
        ? "border-emerald-300 bg-emerald-50 dark:border-emerald-800 dark:bg-emerald-950"
        : "border-red-300 bg-red-50 dark:border-red-800 dark:bg-red-950";
  return (
    <div className={`rounded-md border p-3 ${tone}`}>
      <div className="text-xs text-neutral-600 dark:text-neutral-400">{name}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
    </div>
  );
}

const th = "px-2 py-1.5 text-left font-medium whitespace-nowrap";
const thNum = "px-2 py-1.5 text-right font-medium whitespace-nowrap";
const num = "px-2 py-1.5 text-right tabular-nums";
const row = "border-t border-neutral-200 dark:border-neutral-800";

function FileRow({ entry }: { entry: FileEntry }) {
  return (
    <tr className={row}>
      <td className="px-2 py-1.5 font-mono text-xs whitespace-nowrap">{entry.file}</td>
      <td className="px-2 py-1.5 font-mono text-xs whitespace-nowrap">{entry.pack ?? NONE}</td>
      <td className={num}>{entry.cases}</td>
      <td className={num}>{count(entry.false_allow)}</td>
      <td className={num}>{count(entry.false_block)}</td>
      <td className={num}>{count(entry.wrong_reason)}</td>
      <td className={num}>{entry.mismatches}</td>
    </tr>
  );
}

export default function OpsConsole() {
  const r = evalReport;
  const scenarios = Object.entries(r.scenarios).sort(([a], [b]) => a.localeCompare(b));
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-6 px-4 py-12">
      <header className="flex flex-col gap-1">
        <Link href="/" className="self-start text-sm text-neutral-500 underline">
          AgentPay Guard
        </Link>
        <h1 className="text-xl font-semibold">Ops console: eval scorecard</h1>
        <p className="text-sm text-neutral-600 dark:text-neutral-400">
          How the policy engine scores on every hand-labelled golden case. Built from the
          committed Eval report; no live data.
        </p>
      </header>

      {reportSource.sample ? (
        <p className="rounded-md border border-amber-500 bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100">
          Sample data: this build couldn&apos;t read <code>evals/report.json</code>, so it shows
          the fixture.
        </p>
      ) : null}

      <section aria-label="Totals" className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <Tile name="Decision cases" value={String(r.decision_cases)} />
        <Tile name="False-allow" value={String(r.false_allow)} good={r.false_allow === 0} />
        <Tile name="False-block" value={String(r.false_block)} good={r.false_block === 0} />
        <Tile name="Wrong Reason code" value={String(r.wrong_reason)} good={r.wrong_reason === 0} />
        <Tile name="All mismatches" value={String(r.mismatches)} good={r.mismatches === 0} />
        <Tile name="Step-up rate" value={percent(r.step_up_rate)} />
      </section>

      <p className="text-sm text-neutral-600 dark:text-neutral-400">
        CI fails on any false-allow, false-block or wrong Reason code. Latency isn&apos;t in this
        report because it differs by machine. CI gates it separately: <code>POST /v1/decisions</code>{" "}
        p95 under 150 ms, and the engine&apos;s p99 under 5 ms.
      </p>

      <section className="flex flex-col gap-2">
        <h2 className="font-medium">By scenario</h2>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr>
                <th className={th}>Scenario</th>
                <th className={thNum}>Cases</th>
                <th className={thNum}>Allow</th>
                <th className={thNum}>Step-up</th>
                <th className={thNum}>Block</th>
              </tr>
            </thead>
            <tbody>
              {scenarios.map(([name, c]) => (
                <tr key={name} className={row}>
                  <td className="px-2 py-1.5 whitespace-nowrap">{label(name)}</td>
                  <td className={num}>{c.cases}</td>
                  <td className={num}>{c.allow}</td>
                  <td className={num}>{c.step_up}</td>
                  <td className={num}>{c.block}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="flex flex-col gap-2">
        <h2 className="font-medium">By golden file</h2>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr>
                <th className={th}>File</th>
                <th className={th}>Pack</th>
                <th className={thNum}>Cases</th>
                <th className={thNum}>False-allow</th>
                <th className={thNum}>False-block</th>
                <th className={thNum}>Wrong reason</th>
                <th className={thNum}>Mismatches</th>
              </tr>
            </thead>
            <tbody>
              {r.files.map((entry) => (
                <FileRow key={entry.file} entry={entry} />
              ))}
            </tbody>
          </table>
        </div>
        <p className="text-xs text-neutral-500">
          Dispute and narrative files are scored on network codes, Evidence strength and the
          citation checker&apos;s verdicts, so they show {NONE} for allow and block errors.
        </p>
      </section>

      <footer className="text-xs text-neutral-500">
        {reportSource.release ? (
          <>
            From <code>evals/report.json</code> at <code>{reportSource.release}</code>.{" "}
          </>
        ) : null}
        Regenerated with <code>uv run python -m evals.run_evals --write-report</code>.
      </footer>
    </main>
  );
}
