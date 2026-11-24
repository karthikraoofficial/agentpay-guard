# Razorpay test mode: which demo journeys can be real

_2026-11-24. Research only, from razorpay.com/docs. Nothing here has been checked against the API yet; see "Open questions"._

The hosted demo runs on the mock rail. This note asks which steps of a journey could call real Razorpay **test mode** instead. Repo context: ADR-0012 (one shared test account), ADR-0013 (card recurring, Checkout-made tokens), ADR-0017 (debit 36 h 5 min after the notice; test tokens die at 23:59:59 IST the next day) and ADR-0024 (refunds start in our API).

What the adapter calls today (`adapters/razorpay/rail.py`, `wizard.py`): `POST /customers`, `POST /orders` (registration order with a `token` object and `payment_capture: true`; Payment orders with `notification.token_id`), `GET /orders?receipt=`, `GET /orders/{id}`, `GET /orders/{id}/payments`, `POST /payments/create/recurring`, `POST /payments/{id}/refund` (`speed: normal`), `GET /payments/{id}/refunds`, `GET /customers/{id}/tokens/{id}`. Webhooks handled: `payment.captured`, `payment.failed`, `refund.processed`, `refund.failed`. It makes no dispute calls.

## Summary

| # | Question | Verdict | Why |
|---|---|---|---|
| 1a | Is the card registration payment a real charge that gets captured? | **Yes** (with `payment_capture: true`) | It's a ₹1 order payment. Orders' capture value wins over Dashboard settings, and anything left authorised is auto-refunded after 3 days. |
| 1b | Amount rules | **Yes, documented**: ₹1 for cards and UPI, ₹0 for eMandate | Card order minimum is 100 paise. Registration links must be ₹1. |
| 1c | Test cards for recurring | **Yes**: Visa 4718 6091 0820 4366 (domestic) | Docs say test card tokens last 3 days. We measured about 22 h (#75). |
| 1d | Registration without a browser | **No / Unclear** | Only Checkout or a registration link is documented. S2S JSON needs PCI-DSS, and recurring over S2S isn't documented. |
| 2 | Real refunds in test mode | **Yes for the API. Unclear for timing and webhooks** | Full and partial refunds of captured payments are documented, and the docs say to test refunds in test mode. Nothing says how fast `processed` arrives in test mode, or which `refund.*` events fire there. |
| 3 | Real disputes in test mode | **No (not documented)** | The fetch, accept and contest APIs, the Documents API and the `payment.dispute.*` events are documented. No page says how to create or simulate a dispute in test mode. |
| 4 | UPI Autopay later debit within minutes | **Unclear, the docs contradict each other** | One page says test mode doesn't enforce the 24 h notice window for UPI and debits run "much faster". The same page says UPI Autopay end-to-end testing needs a live account. `success@razorpay` is documented for one-time UPI only. |
| 5 | Another route to a fast later debit | **Partly**: Subscriptions' "Charge this now" (Dashboard only) | It simulates a later charge in test mode, but only from the Dashboard. For eMandate the debit takes up to T+2 working days, and nothing is said about test mode. |

## 1. Card recurring registration via Checkout

**The first payment is real, and captured if you ask for it.** The recurring overview says "The amount field is the first payment charge: ₹1 (100 paise) for UPI and Cards, ₹0 for eMandate" ([overview/integrate](https://razorpay.com/docs/payments/recurring-payments/overview/integrate.md)). "Capture values passed in the Orders API take precedence over the Payment Capture settings configured on the Dashboard" ([capture settings](https://razorpay.com/docs/payments/payments/capture-settings.md)). Our wizard sends `payment_capture: true` on the ₹1 order, so that payment should end `captured`. A payment left `authorized` "is auto-refunded if not `captured` within 3 days of creation" ([Refunds API](https://razorpay.com/docs/api/refunds.md)).
- The overview's refund advice reads oddly: "For auth payments (the ₹1 or ₹0 first payment), set `payment_capture` to manual if you want to avoid auto-refunding. Otherwise, the auth amount is auto-refunded back to the customer after the auto-capture window" ([overview/integrate](https://razorpay.com/docs/payments/recurring-payments/overview/integrate.md)). That conflicts with the capture-settings page, where manual capture is what leads to an auto-refund. Docs are contradictory here. Only a prototype settles what happens with `payment_capture: true`.

**Amount rules.**
- Card orders: "For cards, the minimum value is `100`". Registration links for cards must be ₹1 ([create authorisation transaction, cards](https://razorpay.com/docs/api/payments/recurring-payments/cards/create-authorization-transaction.md)).
- The token's `max_amount` has a minimum of 100. Debits above ₹15,000 (₹1,00,000 for eligible categories) need the customer's AFA. `expire_at` is mandatory on the Orders API. `frequency` is one of `weekly`, `monthly`, `yearly`, `as_presented` (same page).
- Nothing says whether a first charge above ₹1 is allowed on cards. For eMandate, "you can charge any amount to your customer as part of the authorisation transaction" ([eMandate, charge during registration](https://razorpay.com/docs/payments/recurring-payments/emandate/charge-customer-during-registration.md)). Nothing equivalent was found for cards.
- Create the order and the authorisation on the same day, before 11:59 pm ([cards authorisation](https://razorpay.com/docs/api/payments/recurring-payments/cards/create-authorization-transaction.md)).

**Test cards.** The Subscriptions table lists Visa 4718 6091 0820 4366 (domestic), and Mastercard 5104 0155 5555 5558 and 5104 0600 0000 0008 (international). Use any CVV and any future expiry date. Test-mode OTP: 4 to 10 digits succeeds, fewer than 4 fails. "In test mode, you can perform a subsequent debit only within 3 days of token creation, as card tokens are valid for 3 days only" ([test card details](https://razorpay.com/docs/payments/payments/test-card-details.md)). This disagrees with what we measured in #75 (23:59:59 IST the next day, about 22 h; ADR-0013).

**Registration without a browser.** The docs give two routes only: Standard Checkout ("can be created only using Razorpay APIs", which means the order and customer are made by API and the payment in Checkout) and a registration link that the Payer opens ([cards integrate](https://razorpay.com/docs/payments/recurring-payments/cards/integrate/)). The S2S integration page says "PCI-DSS certification is required for all entities seeking to store, process, and transmit cardholder data". It says S2S runs in test mode "against simulated banks, networks and NPCI", but it doesn't cover recurring ([S2S integration](https://razorpay.com/docs/payments/payment-gateway/s2s-integration.md)). Verdict: a Payer in a browser is required. A registration link can be sent by SMS or email, but someone still has to open it.

**36 h 5 min, confirmed by the docs.** "The TAT to create a debit if you send a pre-debit notification is 36 hours and 5 minutes". The error is `BAD_REQUEST_MANDATE_PROMISED_DEBIT_DATE_NOT_HONOURED` ([cards subsequent payments](https://razorpay.com/docs/api/payments/recurring-payments/cards/create-subsequent-payments.md)). That page has no test-mode exception, which matches ADR-0017.

## 2. Refunds

- **Full and partial: yes.** "Make full or partial refunds". Refunds work only on `captured` payments ([Refunds API](https://razorpay.com/docs/api/refunds.md)). Leave out `amount` for a full refund, or pass less for a partial one. Statuses are `pending`, `processed` (final) and `failed`. `receipt` must be unique per payment, and `notes` takes at most 15 pairs ([create normal refund](https://razorpay.com/docs/api/refunds/create-normal.md)). "You can issue multiple partial refunds, as long as their total does not exceed the captured payment amount" ([refunds](https://razorpay.com/docs/payments/refunds.md)).
- **Idempotency header exists.** `X-Refund-Idempotency` takes a key of at least 10 characters. A retry must send the same body, and you get 409 while the first request is still processing. India and Malaysia only ([create normal refund](https://razorpay.com/docs/api/refunds/create-normal.md)). The adapter doesn't send it today: it relies on `receipt` plus notes lookup (ADR-0024).
- **Test mode works.** The overview says "Test the refund flow end-to-end in test mode before launch" ([overview/integrate](https://razorpay.com/docs/payments/recurring-payments/overview/integrate.md)). Test and live "provide the same functionalities, except that real payments cannot be accepted in Test mode" ([test and live modes](https://razorpay.com/docs/payments/dashboard/test-live-modes.md)).
- **Time to `processed` in test mode: docs don't say.** Live normal refunds take 5–7 working days, and instant refunds are near-immediate ([refunds](https://razorpay.com/docs/payments/refunds.md)).
- **Webhooks.** The events are `refund.created`, `refund.processed`, `refund.failed` and `refund.speed_changed` ([refund webhooks](https://razorpay.com/docs/payments/refunds/subscribe-to-webhooks.md)). For test mode in general: "A Test mode webhook receives events for your test transactions" ([webhooks](https://razorpay.com/docs/webhooks.md)) and "Test events get triggered on a transaction done in the Test mode" ([validate and test](https://razorpay.com/docs/webhooks/validate-test.md)). No page lists which events fire in test mode, so whether the refund events do is unclear from the docs.

## 3. Disputes

- **APIs:** `GET /v1/disputes`, `GET /v1/disputes/:id` (with `expand[]=payment` or `transaction.settlement`), `POST /v1/disputes/:id/accept` and `PATCH /v1/disputes/:id/contest` ([Disputes API](https://razorpay.com/docs/api/disputes.md)). Contest takes `amount`, a `summary` of up to 1000 characters, and lists of document ids (`shipping_proof`, `billing_proof`, `proof_of_service`, `explanation_letter`, `others`, and more). `action` is `draft` (the default, status stays `open`) or `submit` (status goes to `under_review` and fires `payment.dispute.under_review`) ([contest](https://razorpay.com/docs/api/disputes/contest.md)). Evidence goes up through `POST /v1/documents` with purpose `dispute_evidence` ([documents](https://razorpay.com/docs/api/documents.md); purpose from the contest page).
- **Webhooks:** `payment.dispute.created`, `.won`, `.lost`, `.closed`, `.under_review` and `.action_required` ([dispute webhooks](https://razorpay.com/docs/webhooks/disputes.md)).
- **Phases and statuses:** the phases are fraud, retrieval, chargeback, pre-arbitration and arbitration. The statuses are open, under review, won, lost and closed ([about disputes](https://razorpay.com/docs/payments/disputes.md)).
- **Test mode: docs don't say how to get a dispute.** The about, dashboard, APIs and FAQ pages say nothing about creating or simulating a dispute in test mode ([about](https://razorpay.com/docs/payments/disputes.md), [dashboard](https://razorpay.com/docs/payments/disputes/dashboard.md), [APIs](https://razorpay.com/docs/payments/disputes/apis.md), [FAQs](https://razorpay.com/docs/payments/disputes/faqs.md)). The API pages only say you can try the endpoints from Postman with test keys. There is no documented create-dispute API, and no documented test trigger. The `payment.dispute.*` events in test mode are therefore undocumented too.

## 4. UPI Autopay in test mode

- **Live rules:** the pre-debit notice goes 24 h ahead, and "subsequent charges are initiated 25 hours after the Pre Debit notification is sent" ([UPI FAQs](https://razorpay.com/docs/payments/recurring-payments/upi/faqs.md)). In the decoupled flow (sending a `notification` object), you create the payment once "a minimum of 24 hours has passed since the PDN was sent". Without the object, Razorpay debits 25 h after delivery and retries 3 times. "NPCI allows only one successful debit on a token per billing cycle" ([UPI subsequent payments](https://razorpay.com/docs/api/payments/recurring-payments/upi/create-subsequent-payments.md)).
- **Test mode, contradictory:** the overview says "Test mode does not enforce the 24-hour Pre-Debit Notification window for UPI" and "Debits queue and execute much faster in test mode than they will in production". The same page also says "UPI Autopay end-to-end testing requires a live merchant account, since UPI test values are not available in test mode" ([overview/integrate](https://razorpay.com/docs/payments/recurring-payments/overview/integrate.md)).
- **Test VPAs:** `success@razorpay` and `failure@razorpay` are documented for one-time UPI only. The page doesn't mention Autopay. It also says a cancelled UPI payment in test mode counts as a success ([test UPI details](https://razorpay.com/docs/payments/payments/test-upi-details.md)).
- **Token expiry:** `expire_at` defaults to 10 years, with a maximum of 30 ([UPI authorisation](https://razorpay.com/docs/api/payments/recurring-payments/upi/create-authorization-transaction.md)). No test-mode expiry is documented.
- **No browserless registration:** only Checkout or a registration link is documented, and no UPI S2S create endpoint appears. UPI Collect is closed to new registrations from 28 Feb 2026, apart from listed exemptions (same page).

## 5. Other routes to "pays later, within minutes"

- **Subscriptions:** "In test mode, you can simulate these charges from the Dashboard using the Charge this now button". A test charge fires `subscription.charged`, or `subscription.activated` then `subscription.charged`. The same page says card tokens last 3 days in test mode ([test subscriptions](https://razorpay.com/docs/subscriptions/test-guide/)). It's a Dashboard button, and no API equivalent is documented. A Subscription is a fixed Plan schedule, not "the Agent picks an amount and a time".
- **eMandate / NACH:** the authorisation amount is ₹0 ([overview](https://razorpay.com/docs/payments/recurring-payments/overview/integrate.md)). The token is confirmed in real time for NPCI eNACH, ICICI and Axis, and T+1 for HDFC and SBI netbanking. Debits take up to T+2 working days, with ICICI real-time ([eMandate FAQs](https://razorpay.com/docs/payments/recurring-payments/emandate/faqs.md)). In test mode, creating a recurring payment returns `razorpay_payment_id`, `razorpay_order_id` and `razorpay_signature` ([eMandate subsequent payments](https://razorpay.com/docs/api/payments/recurring-payments/emandate/create-subsequent-payments.md)). No test-mode timing is documented.
- **UPI Reserve Pay:** no docs page on test mode was found (as ADR-0013 already says).

## Implications for real demo journeys

| Step | Feasible in test mode? | Notes |
|---|---|---|
| Real Consent via Checkout (card token) | **Yes** | It's what `adapters.razorpay.wizard` already does. It needs a browser and the test card, and the token dies about 22 h later. |
| Real capture | **Yes, but only of the ₹1 registration payment** | An Agent-initiated Payment (a later debit) never captures on card recurring in test mode (ADR-0017). One option is a journey that treats the registration charge itself as the captured payment. That doesn't fit our Payment model, where a Payment is one Decision's debit. |
| Real refund | **Likely, of the ₹1 registration payment** | The API supports it on any captured payment. How fast it reaches `processed` and whether webhooks fire are undocumented. Capped at ₹1. |
| Real dispute | **No** | There is no documented way to raise one in test mode. Keep disputes on the mock rail. |
| Real later debit within minutes | **No on cards. Unclear on UPI Autopay and eMandate** | The UPI test-mode claim ("PDN window not enforced") is the only lead, and the same page contradicts it. |

So the realistic "real" journey today is: real Checkout consent, then a real ₹1 capture, then a real refund. The Agent's own Payment, its later debit and any Dispute stay on the mock rail, or a hybrid: real token, mock debit.

## Open questions to settle with a prototype

1. With `payment_capture: true` on the card registration order, does the ₹1 payment end `captured` in test mode, and does it ever auto-refund?
2. Is a card registration order above ₹1 accepted?
3. How long does a test-mode normal refund take to reach `processed`? Do `refund.created` and `refund.processed` arrive at the test webhook? Is `X-Refund-Idempotency` honoured in test mode?
4. Does the test Dashboard offer any way to raise a dispute on a test payment? (Check the Disputes tab in test mode, or ask Razorpay support.)
5. UPI Autopay in test mode: can a mandate be registered with `success@razorpay` at all, and how soon after the notice does the decoupled `payment_after` debit go through?
6. Is a card token's test-mode life 3 days (the docs) or until 23:59:59 IST the next day (our #75 measurement)? Re-measure once.
7. eMandate in test mode: how soon after registration is the token `confirmed`, and how soon does a debit settle?
