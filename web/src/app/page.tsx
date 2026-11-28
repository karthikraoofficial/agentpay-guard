import Link from "next/link";

import { JourneyPicker } from "./journey-picker";
import { PITCH_ID, playlistById } from "@/lib/playlists";

export default function Home() {
  const pitch = playlistById(PITCH_ID);
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-8 px-4 py-12">
      <header className="flex flex-col gap-3">
        <h1 className="text-2xl font-semibold">AgentPay Guard</h1>
        <p className="text-neutral-700 dark:text-neutral-300">
          A trust layer for payments an AI assistant makes: before it pays, it asks the Guard,
          which allows, asks the person first, or blocks, and keeps the proof.
        </p>
      </header>

      <section aria-labelledby="try" className="flex flex-col gap-3 rounded-lg border border-sky-300 bg-sky-50 p-5 dark:border-sky-800 dark:bg-sky-950">
        <h2 id="try" className="text-lg font-semibold">
          Try the demo
        </h2>
        {pitch ? (
          <>
            <p className="text-sm">
              <span className="font-medium">{pitch.title}</span> ({pitch.length}): {pitch.story}
            </p>
            <p className="text-xs text-neutral-600 dark:text-neutral-400">
              Journeys {pitch.journeys.join(" → ")}. The conversation is scripted, but every call
              to the Guard is real. No real money moves.
            </p>
            <Link
              href={`/demo?playlist=${pitch.id}`}
              className="self-start rounded-md bg-sky-700 px-4 py-2 text-sm font-medium text-white hover:bg-sky-800"
            >
              ▶ Try the demo
            </Link>
          </>
        ) : (
          <Link href="/demo" className="self-start underline">
            Open the demo
          </Link>
        )}
      </section>

      <section className="flex flex-col gap-4" aria-label="Every journey">
        <h2 className="text-lg font-semibold">Or pick a journey</h2>
        <JourneyPicker />
      </section>

      <footer className="flex flex-col gap-2 border-t border-neutral-200 pt-4 text-sm dark:border-neutral-800">
        <p>
          <Link href="/how-it-works" className="underline">
            How AgentPay Guard works
          </Link>
          : a map of how a payment is decided, how the person is asked, and how a dispute is
          handled.
        </p>
        <p>
          The{" "}
          <Link href="/ops" className="underline">
            Ops console
          </Link>{" "}
          shows how the policy engine scores on every hand-labelled golden case.
        </p>
        <p className="text-neutral-600 dark:text-neutral-400">
          Payers reach the consent screen through the Consent link their provider sends them,
          and reviewers reach the dispute viewer through a Review link.
        </p>
      </footer>
    </main>
  );
}
