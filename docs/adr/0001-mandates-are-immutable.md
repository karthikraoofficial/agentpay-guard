# Mandates are immutable; changes create a superseding Mandate

A Mandate's terms (agents, caps, MCCs, validity) never change once it is created. There is no update endpoint. To change terms, the Payer revokes the old Mandate and grants a new one that records `supersedes`. Revocation is terminal and takes effect for every decision evaluated after it commits.

We chose this over in-place edits because every Decision and dispute evidence pack cites a Mandate, and replay must re-run against exactly the terms that applied at the time. With mutable Mandates we would need a version history and point-in-time reads to get the same guarantee.
