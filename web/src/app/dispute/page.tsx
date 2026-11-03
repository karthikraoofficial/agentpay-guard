import type { Metadata } from "next";

import { DisputeViewer } from "./dispute-viewer";

export const metadata: Metadata = {
  title: "Review a dispute · AgentPay Guard",
  // A review page holds a payment's evidence: keep it out of search indexes and never
  // send its URL on as a referrer.
  robots: { index: false, follow: false },
  referrer: "no-referrer",
};

// The Review link token is in the URL fragment, which only the browser can read, so the
// viewer is a Client Component and this page renders nothing request-specific.
export default function DisputePage() {
  return <DisputeViewer />;
}
