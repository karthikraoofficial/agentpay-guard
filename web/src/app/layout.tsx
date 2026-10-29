import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AgentPay Guard",
  description: "Consent, operations and dispute views for agent-initiated payments.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
