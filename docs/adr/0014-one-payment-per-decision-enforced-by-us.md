# One Payment per Decision, enforced by our database rather than the rail

Razorpay has no idempotency key for creating orders or payments. So a Payment row, unique per Decision, is committed before any rail call, and the Razorpay order's `receipt` carries our Payment id. A retry after an unclear outcome (timeout or 5xx) looks the order up by that receipt and never creates a second one. A Payment is always for exactly its Decision's amount, with no partial or split Payments.

This makes "a blocked Decision never reaches the rail" and "nothing is charged twice" properties of our own data, which tests can prove without the rail. The cost is one extra lookup on retry, and a rule that a Payment's amount cannot be adjusted, even by a few paise.

## Consequences

- Razorpay finds orders by `receipt` through a search index that lagged about 25 s behind order creation when measured on the test account in October 2026, and listing recent orders lagged the same way. Fetching an order by id is immediate, but after a lost answer we have no id. So each rail declares a lookup lag, 2 minutes for Razorpay and none for the mock rail, and nothing asks the rail about a Payment younger than that: a same-key retry replays until then, and the reconciliation sweep waits at least that long. Without this, a quick retry could find no order and make a second one, and the customer would get a second Pre-debit notice.
