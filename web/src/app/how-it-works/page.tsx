import type { Metadata } from "next";
import Link from "next/link";

import { FlowMap } from "./flow-map";

export const metadata: Metadata = {
  title: "How AgentPay Guard works",
  description: "A map of how a payment by an AI agent is decided, how the person is asked, and how a dispute is handled.",
};

export default function HowItWorks() {
  return (
    <main className="mx-auto flex w-full max-w-6xl flex-1 flex-col gap-6 px-4 py-12">
      <header className="flex flex-col gap-1">
        <Link href="/" className="self-start text-sm text-neutral-500 underline">
          AgentPay Guard
        </Link>
        <h1 className="text-xl font-semibold">How AgentPay Guard works</h1>
        <p className="max-w-3xl text-sm text-neutral-600 dark:text-neutral-400">
          When an AI agent wants to pay for something on a person&apos;s behalf, AgentPay Guard
          decides in a fraction of a second whether to allow it, ask the person first, or block
          it. If the payment is later disputed, it puts together the evidence. Click any block
          below to see what it does.
        </p>
      </header>
      <FlowMap />
    </main>
  );
}
