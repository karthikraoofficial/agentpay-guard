# web/

Next.js (App Router + Tailwind) front end for AgentPay Guard. Phase 3 adds the consent
screen; the ops console and the dispute pack viewer come later.

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
