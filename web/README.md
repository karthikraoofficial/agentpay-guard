# web/

Next.js (App Router + Tailwind) front end for AgentPay Guard: the consent screen (Phase 3),
the dispute viewer (Phase 5), the Ops console (Phase 6) and the demo page (`/demo`).

```bash
cp .env.example .env.local   # NEXT_PUBLIC_API_URL and NEXT_PUBLIC_DEMO_RUNNER_URL
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

## The demo page (`/demo`)

A visitor picks a journey, grouped A–H, and watches it play. The page has three panes,
which become tabs at phone width:
- **Chat:** Asha's and ShopBot's scripted lines. After each Guard call comes a tool-call
  card (the MCP tool name or HTTP route, with the request and response) and, for a
  Decision, a verdict card with the Outcome and every Reason code in plain words. In H8 a
  webhooks card lists each Tenant webhook the wallet app received about the run, each
  with its signature verified by the Demo runner's receiver.
- **Asha's phone:** on a step_up, the real consent screen in a phone frame, opened on the
  run's Consent link (`/consent#<token>`, ADR-0008). The visitor approves or declines as
  Asha, and the chat carries on with her answer, including a blocked approval's reason.
  At phone width the page switches to this tab while Asha must answer, and back after.
- **Guard inspector:** the latest Decision's Outcome, Reason codes, Policy pack version,
  Pack hash, Decision id and Audit record id. "What to notice" shows at the end.

The visitor can step one turn at a time or press Play. A step that waits on the worker
(a Payment being taken) is polled until it moves on.

- **The calls are real.** The page calls the Demo runner (ADR-0031) at
  `NEXT_PUBLIC_DEMO_RUNNER_URL`: `POST /runs` to start a run, then
  `POST /runs/{id}/next` for each step. The runner holds the Demo Tenant's keys, so the
  browser never sees one, and only this app's origin (`AGENTPAY_WEB_ORIGIN`) may call it.
  A 429 from the runner shows as "The demo is busy. Try again in N seconds."
- **Fast-forward.** A step that waits on a timer can show a ⏩ button. Pressing it asks
  the runner to use a Demo control (ADR-0032), which ends that one timer for real.
- **Switches.** A step can show a ⚡ button that forces a failure through a Demo control
  (ADR-0032). While a simulated bank outage runs, every open page shows a banner, from
  the runner's `GET /status`, checked every 10 seconds.
- **The inspector** shows the Decision's Rule results and Pack hash. Its Verify / Replay
  button asks the runner to replay this run's Decision and walk the Demo Tenant's Audit
  chain, and shows both answers.
- **The catalogue is fixed at build time.** Before `dev`, `lint`, `typecheck` and
  `build`, `scripts/sync-scenarios.mjs` checks every `../demo/scenarios/*.json` against
  `../demo/scenario.schema.json` (with Ajv) and copies them into `src/data/scenarios.json`
  (git-ignored). A malformed Scenario file fails the build. If the build can't see
  `../demo`, the page lists no journeys.
- **Reason code text** is in `src/lib/reason-codes.ts`; a Python test
  (`tests/test_web_reason_codes.py`) fails if a Reason code is missing from it.

Locally: run the API with mock settle (`AGENTPAY_MOCK_SETTLE_AFTER_SECONDS`), plus
`AGENTPAY_DEMO_CONTROLS_TENANT_ID` and `AGENTPAY_MOCK_NOTICE_SECONDS` for the fast-forward
journeys, the worker, and the Demo runner (`uv run python -m demo_runner`, port 8100) with the Demo
Tenant's keys, then `npm run dev` and open http://localhost:3000/demo.

## The Ops console (`/ops`)

A read-only eval scorecard. It shows the Decision case count, false-allows, false-blocks,
wrong Reason codes, the step-up rate, the counts per scenario and per golden file, and the
release the numbers came from. It needs no API key and shows no live data.

- It's built from the committed Eval report. Before `dev`, `lint`, `typecheck` and
  `build`, `scripts/sync-eval-report.mjs` copies `../evals/report.json` into
  `src/data/eval-report.json` (git-ignored), with `git describe` of the checkout. The page
  is then prerendered as static HTML.
- If the build can't see `../evals/report.json`, for example because a Vercel project
  doesn't include files outside `web/`, it uses `src/data/eval-report.fixture.json` and
  the page says it is sample data.
- Latency isn't on it, since it differs by machine; CI gates latency separately.

To refresh it, regenerate the report (`uv run python -m evals.run_evals --write-report`)
and rebuild.
