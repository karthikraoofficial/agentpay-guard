# Refunds start only in our API, by the Tenant, keyed to the rail by Refund id

Until now a Payment, once captured, could never give money back through AgentPay Guard (#205). A refund made in the rail's own dashboard would be invisible to us. On our one shared rail account it can't even be traced to a Tenant (ADR-0012). So the Monthly cap, the Audit chain and any later Evidence pack would know nothing of it.

So a Refund is made only through `POST /v1/payments/{id}/refunds`, with a `manage` key:
- **The Tenant only.** An Agent's `decide` key is refused, so an Agent never moves money back on its own.
- **Like a Payment, between two transactions (ADR-0014).** The Refund is stored `pending` with its `refund` Audit record first. Then the rail is asked, keyed by the Refund's own id. Then the answer is recorded with a second `refund` Audit record. A timeout leaves the Refund pending, to be asked about again by its id, never one the rail made that we don't know of. Asking again for the same Refund never refunds twice.
- **Never more than was captured.** Pending and processed Refunds count towards the captured amount, failed ones don't. The check runs with the Mandate and then the Payment locked, the same order rail reports and Disputes use, so two refunds at once queue and can't overshoot.
- **Not while disputed.** A Payment with an open, in-review or ready Dispute is refused with `PAYMENT_DISPUTED`. Otherwise a refund on top of a chargeback would pay the Payer twice. A withdrawn or expired Dispute no longer blocks.
- **Inside the Refund window.** The window is set in the Policy pack (180 days from `default@1.5.0`; older packs read as the same) and counted from capture. The `refund` Audit record names the pack and Pack hash that allowed it.
- **The Payment stays `captured`.** Its Refunds and the processed total are shown beside it, so "captured" keeps meaning the money moved and every existing consumer keeps working.
- **No words in the chain.** The Tenant's optional reason is kept on the Refund, and only its hash goes into the chain (ADR-0007).

A refund event from the rail that names no Refund of ours, such as one made in the dashboard, is logged and dropped (#210). That changes only with a connected account per Tenant (Razorpay Route).

We considered letting an Agent refund its own Payment, for example to cancel an order. That needs rules about when an Agent may move money back, and an Agent's Mandate covers paying, not refunding. We also considered a `refunded` Payment status, but every reader of `captured` would have had to learn it, and a partial refund fits neither status.
