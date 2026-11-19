# Decisions never depend on rail availability; payments fail fast through a circuit breaker

The go-live checklist said a rail outage should "fail to step_up or block, never allow". Decided on #223: that is not how risk decisions should work, here or elsewhere.

A Decision says whether an Agent may pay. It never calls the rail, and an allow moves no money until its Payment is made. Payment risk engines decide on the payment's risk, not on the processor's health. Card networks, when an issuer is down, decide for it under pre-agreed limits rather than change the risk call. Processor outages are handled where money moves, with idempotency keys, retries, circuit breakers and failover. We follow that:

- **Decisions keep their Outcomes during a rail outage.** Stepping up instead would add no safety, because the allow can't move money while the rail is down. It would ask the Payer to approve a payment that still can't be made. And it would make rail health, which changes minute to minute, an input that every Decision's Audit record would have to hold for byte-for-byte replay (non-negotiable 5).
- **The payment layer fails safe.** That was already the case. Every rail call is keyed by our Payment or Refund id (ADR-0014, ADR-0024). An unknown outcome leaves a Payment `scheduled` or a Refund `pending`. The sweep settles it, or fails one the rail never saw, never charging it late (ADR-0016).
- **And now it fails fast (#226).**
  - **Opening:** after `AGENTPAY_RAIL_BREAKER_FAILURES` (5) unknown outcomes in a row from the rail, a circuit breaker opens for `AGENTPAY_RAIL_BREAKER_OPEN_SECONDS` (30).
  - **While open:** `POST /v1/payments` and `POST /v1/payments/{id}/refunds` answer `503 RAIL_UNAVAILABLE` with `Retry-After`. That comes after the request's own checks and before anything is written, so the Decision stays payable within its Payment window and the Idempotency-Key stays free.
  - **Trying again:** after the open period, one request is let through to try the rail. Its answer closes the breaker, and another unknown outcome reopens it.
  - **What counts:** only unknown outcomes. A refusal by the rail (`failed` with a code) is an answer, and resets the run.

The breaker's state is per API process. That is enough for one API on one VM (ADR-0021). Several API processes would each keep their own, which still fails fast, only a little later.

The worker's debit step and sweep don't use the breaker. They already run at a set interval, and they own Payments the API has handed over.
