# Mock settle captures demo Payments, and never runs on a real rail

Demo journey 3 needs a captured Payment to dispute. On the hosted demo the rail is MockRail, which keeps its charges inside the API process and sends no webhook, so a Payment stays `scheduled` forever and no Dispute can be opened. Razorpay test mode can't help either: a test token expires before its debit is allowed (ADR-0017).

So the hosted demo sets `AGENTPAY_MOCK_SETTLE_AFTER_SECONDS` (about 60). The worker's reconciliation sweep then asks a settling mock rail about each scheduled mock Payment older than that. That rail answers from the stored Payment: debited means captured. The sweep applies the answer through the same transition a rail webhook uses, so the capture writes the same Audit record, outbox event and Tenant webhook as a real one. A mock Payment that was never debited is failed with `RAIL_CHARGE_MISSING`, as on any rail (ADR-0016).

This settles money the way no real rail would: every debited mock Payment is captured, including one whose mock token would fail. That is acceptable only because no money moves on the mock rail. Settings refuse the setting with any other rail, so it can't capture a real Payment. Unset, which is the default, nothing changes: mock Payments stay `scheduled` and the sweep stays off on the mock rail.

We considered an admin command that captures one Payment by hand. It would make the demo depend on a terminal on the server, and it would be a second capture path, besides the webhook and the sweep, that the tests don't already cover.
