# One Payment per Decision, enforced by our database rather than the rail

Razorpay has no idempotency key for creating orders or payments. So a Payment row, unique per Decision, is committed before any rail call, and the Razorpay order's `receipt` carries our Payment id. A retry after an unclear outcome (timeout or 5xx) looks the order up by that receipt and never creates a second one. A Payment is always for exactly its Decision's amount, with no partial or split Payments.

This makes "a blocked Decision never reaches the rail" and "nothing is charged twice" properties of our own data, which tests can prove without the rail. The cost is one extra lookup on retry, and a rule that a Payment's amount cannot be adjusted, even by a few paise.
