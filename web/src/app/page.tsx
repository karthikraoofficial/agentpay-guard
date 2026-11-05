import Link from "next/link";

export default function Home() {
  return (
    <main className="mx-auto flex max-w-2xl flex-1 flex-col justify-center gap-3 px-4 py-16">
      <h1 className="text-2xl font-semibold">AgentPay Guard</h1>
      <p>
        Payers reach the consent screen through the Consent link their provider sends them,
        and reviewers reach the dispute viewer through a Review link.
      </p>
      <p>
        The{" "}
        <Link href="/ops" className="underline">
          Ops console
        </Link>{" "}
        shows how the policy engine scores on every hand-labelled golden case.
      </p>
    </main>
  );
}
