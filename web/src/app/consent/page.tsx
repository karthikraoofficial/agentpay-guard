import type { Metadata } from "next";

import { ConsentScreen } from "./consent-screen";

export const metadata: Metadata = {
  title: "Approve a payment · AgentPay Guard",
  // A consent page is personal and short-lived: keep it out of search indexes and never
  // send its URL on as a referrer.
  robots: { index: false, follow: false },
  referrer: "no-referrer",
};

// The Consent link token is in the URL fragment, which only the browser can read, so the
// screen is a Client Component and this page renders nothing request-specific.
export default function ConsentPage() {
  return <ConsentScreen />;
}
