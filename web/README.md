# web/

Next.js (App Router + Tailwind) front end for AgentPay Guard: the consent screen (Phase 3)
and the dispute viewer (Phase 5). The ops console comes later.

```bash
cp .env.example .env.local   # NEXT_PUBLIC_API_URL, default http://localhost:8000
npm ci
npm run dev                  # http://localhost:3000
npm run lint
npm run typecheck            # next typegen && tsc --noEmit
npm run build
```

Uses system fonts on purpose, so builds need no network access.

## The consent screen (`/consent`)

A Payer opens the Consent link their Tenant delivered: `<web origin>/consent#<token>`.
The token sits in the URL fragment, which browsers never send to a server. The page reads
it in the browser and sends it to the API in a `Consent-Link` header (ADR-0008).

- It shows what the agent wants to buy, why it was flagged, the Intent verdict, how long
  is left, and a warning if the mandate has stopped. All of this comes from the API's view
  of the link. It never shows the agent's own explanation.
- Approve and Decline each send one `Idempotency-Key` per decision. A double tap or a
  retry therefore resolves the request once. The page then shows the result: approved,
  declined, or blocked with the reason.
- An expired, used, replaced or unknown link shows a plain message and no buttons.

To try it locally:
1. Run the API with `AGENTPAY_WEB_ORIGIN=http://localhost:3000`.
2. Create a step_up and mint its link with `POST /v1/consent-requests/{id}/link`.
3. Open the returned URL.

## The dispute viewer (`/dispute`)

A reviewer at the Tenant opens a Review link: `<web origin>/dispute#<token>`. As with the
consent screen, the token stays in the fragment and goes to the API in a `Review-Link`
header.

- It shows the Dispute (reason, respond-by, payment, reviewer), the assessment (Evidence
  strength, Visa and Mastercard codes, what the evidence shows and what is missing), and
  the narrative. Each claim's citations open to the Audit record they cite. A narrative
  written by code because the model's was refused is flagged.
- **Sign off** approves the version on screen, as the reviewer the link names, and uses
  the link up. **Regenerate** asks for a new version; the page checks back every few
  seconds until it is built. Each sends one `Idempotency-Key` per action and version, and
  a refusal (a newer version exists, the version limit, a closed Dispute) is shown in
  words.
- **Print or save as PDF** uses a print stylesheet: no buttons, every claim with its
  citation ids, and an appendix of every cited Audit record.
- An expired, used, replaced or unknown link, or one for a closed Dispute, shows a plain
  message and no buttons.

To try it locally:
1. Run the API with `AGENTPAY_WEB_ORIGIN=http://localhost:3000`.
2. Open a Dispute on a captured Payment, let the worker build its pack, and mint a link
   with `POST /v1/disputes/{id}/review-link`.
3. Open the returned URL.
