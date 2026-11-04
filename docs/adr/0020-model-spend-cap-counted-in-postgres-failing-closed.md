# The Model spend cap is counted in Postgres and fails closed

The hosted demo is public, and every Intent check and dispute narrative is a paid model call. A leaked agent key could call `POST /v1/intents` in a loop. So each Tenant gets a daily limit on model calls per purpose: by default 200 Intent checks and 20 dispute narratives per UTC day (`AGENTPAY_INTENT_CHECK_DAILY_CAP`, `AGENTPAY_DISPUTE_NARRATIVE_DAILY_CAP`).

A wrapper around ModelGateway counts each call before making it. Over the limit it doesn't call the model, traces the call with outcome `capped`, and raises `ModelSpendCapReached`. That error is a `ModelUnavailable`, so every caller already fails closed: the Intent verdict is `unknown` with failure cause `capped`, so a Decision citing it steps up, and the narrative is written by code. The model is never on the decision path, so Decisions and their replay don't change. A purpose with no limit is never called.

The count lives in Postgres, in one row per Tenant, purpose and day, taken with a single conditional upsert, so two parallel calls can't both take the last one. An in-process counter would reset on every deploy and wouldn't be shared by the API and the worker, which both make model calls. Each count commits on its own, outside the Tenant transaction around the call. The Intent check runs inside the request's transaction, and a call costs money even if that transaction later rolls back.

A call that fails or times out still counts. That's deliberate, because a slow provider is no reason to allow more calls.
