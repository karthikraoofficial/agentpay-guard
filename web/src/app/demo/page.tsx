import type { Metadata } from "next";

import { DemoStudio } from "./demo-studio";

export const metadata: Metadata = {
  title: "Demo · AgentPay Guard",
  description:
    "Watch an AI shopping assistant ask AgentPay Guard before it pays: real calls, real Decisions, real Audit records.",
};

// Runs are started from the browser through the Demo runner, so the page itself renders
// only the catalogue, which is fixed at build time.
export default function DemoPage() {
  return <DemoStudio />;
}
