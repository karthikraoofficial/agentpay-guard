# The policy engine is a pure function; time and spend are passed in

`decide(request, mandate, spent_this_month, pack, now)` does no I/O, reads no clock and creates no IDs. The API layer reads the time once from a `Clock` port and sums Spent this month from the store, then passes both in as values and records them in the Audit record. Replay feeds the recorded values back in.

We rejected injecting a Clock or a spend-lookup port into the engine. The values would be just as fakeable in tests, but replay would then have to stub ports to reproduce the original answer, and a later payment could quietly change a replayed result. Passing values makes "same inputs, same bytes out" true by construction.
