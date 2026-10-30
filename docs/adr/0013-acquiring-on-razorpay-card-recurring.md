# The first real rail is Razorpay card recurring payments, as an acquirer

AgentPay Guard decides first, then charges the Payer's Rail token on the Tenant's behalf (acquiring). The first real rail is Razorpay test mode using card recurring payments.

## Considered Options

- **Issuing** (the Agent holds a virtual card and the issuer asks us at authorisation time, as with Stripe Issuing). The rail would confirm the merchant's MCC. Rejected because it reverses the flow that Phases 1–3 built (the Decision would be triggered by the rail, under a ~2 s deadline) and it depends on Issuing access.
- **Stripe test mode.** Simpler to test headless, with native idempotency keys. Rejected in favour of Razorpay for the Indian-first story and an account already in hand.
- **UPI Reserve Pay** (single block, multiple debits). The best fit for agent payments in India, but it needs activation by Razorpay support and has no documented test mode. It is the target for a later swap behind the same port.

## Consequences

- The Declared MCC can never be confirmed, because the rail only knows the Merchant of record.
- No debit is instant. RBI e-mandate rules need a pre-debit notice, so a Payment is scheduled and settles later by webhook.
- Rail tokens are created by the Payer through Checkout, never by our API. Test-mode tokens expire after 3 days.
