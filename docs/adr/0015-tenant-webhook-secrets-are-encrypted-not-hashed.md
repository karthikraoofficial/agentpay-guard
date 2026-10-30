# Tenant webhook signing secrets are stored encrypted, not hashed

API keys are stored only as hashes, because we only ever check them. A Tenant webhook signing secret is different: we sign every outbound Tenant webhook with it, so we need the secret itself. It is shown to the Tenant once and stored encrypted with a key from `core/settings.py`. It never appears in logs or API responses.

Non-negotiable 7 forbids storing a Payer's card numbers, UPI PINs or raw credentials. This secret is one we issue, scoped to signing, and rotatable by the Tenant. Asymmetric signing (we keep a private key, Tenants verify with a public key) would avoid storing per-Tenant secrets. It was rejected for the MVP because HMAC is what Tenants expect from Razorpay and Stripe, and it is simpler to verify.
