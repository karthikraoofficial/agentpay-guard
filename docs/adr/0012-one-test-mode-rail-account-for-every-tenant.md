# One test-mode rail account serves every Tenant

For the MVP, AgentPay Guard charges through a single rail account of its own, in test mode, with its key set through `core/settings.py`. The app refuses to start if that key looks live, so "test mode only" is enforced in code rather than by convention.

The obvious alternative was per-Tenant rail keys, which is what production needs. It was rejected for now because holding a Tenant's secret rail key is holding a raw credential (non-negotiable 7). The production answer is a platform account with one connected account per Tenant (such as Stripe Connect or Razorpay Route), where we hold only an account reference. Every `PaymentRail` method already takes a `tenant_id`, so that swap happens behind the port and leaves callers unchanged.

## Consequences

- Every Tenant's test payments appear in one rail dashboard. Rail events are matched back to a Tenant by our own references, never by the rail account.
- A rail-side refund or dispute cannot be attributed to a Tenant from the rail account alone.
