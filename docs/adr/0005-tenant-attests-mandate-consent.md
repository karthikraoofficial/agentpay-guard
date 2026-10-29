# The Tenant attests Payer consent to a Mandate; we record it, not verify it

`POST /v1/mandates` requires `consent_evidence` (method, reference, captured_at) supplied by the Tenant. AgentPay Guard stores it on the Mandate and in every Audit record's Mandate snapshot, but does not check it against the Payer, because the Tenant owns the Payer relationship and the channel consent was given in.

This sets the liability boundary. If the attestation was false, the loss is the Tenant's. AgentPay Guard is responsible for deciding correctly against the Mandate terms it was given. Verifying consent ourselves (for example, by confirming a UPI mandate reference with the rail) is a later option. It would add to the evidence fields, not replace them.
