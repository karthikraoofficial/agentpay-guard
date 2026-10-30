# A card debit is two steps, 36 hours apart

On Razorpay card recurring payments, a debit can't be made when the Agent pays. Indian rules need a pre-debit notice to the Payer first, and Razorpay refuses the debit until 36 hours and 5 minutes after the notice. This holds in test mode too (findings from the #75 prototype, posted on #74). So a Payment is made in two steps:

1. **At payment time,** `POST /v1/payments` saves the Payment and asks the rail to schedule it. Razorpay creates the order with its pre-debit notice and returns `debit_after`. The Payment is `scheduled`.
2. **When `debit_after` passes,** the worker checks again that the Mandate is active and the Agent enabled, then makes the debit. If either has changed, it fails and Voids the Payment instead. A webhook, or the reconciliation sweep, settles it as captured or failed.

The Payment window (15 minutes) limits only step 1, when the Agent may start paying. It doesn't cover the debit itself, which the rail controls. Checking the Mandate again at step 2 keeps "a Revocation stops even an allow that was already given" true across the day and a half.

## Considered Options

- **Debit at payment time.** Not possible on this rail: Razorpay refuses it.
- **Send the notice at Decision time instead.** That would notify the Payer about payments the Agent may never make, and the 15-minute window would still be far shorter than the notice period.

## Consequences

- In test mode a card debit can never complete. Test tokens expire at 23:59:59 IST the day after registration (about 22 hours), before step 2 is allowed. Contract tests against Razorpay cover step 1 only, and the full flow runs on MockRail, which debits at once.
- A Payment can sit in `scheduled` for a day and a half, so `scheduled` means "booked", not "about to settle".
