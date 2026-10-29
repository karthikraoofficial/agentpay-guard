# The policy engine is a pure function; time, spend and signals are passed in

`decide(request, mandate, agent, spent_this_month, signals, pack, now)` does no I/O, reads no clock and creates no IDs. The API layer reads the time once from a `Clock` port, reads the Mandate and Agent, and sums Spent this month from the store. It also works out the Signals: Velocity and New merchant from the store, and the Intent verdict from its stored record. It passes all of them in as values and records them in the Audit record. An architecture test allows the engine to import only domain types and the pack model. Replay feeds the recorded values back in, and never recomputes a Signal or calls a model again.

We rejected injecting a Clock, a spend-lookup port or a signal-lookup port into the engine. The values would be just as fakeable in tests, but replay would then have to stub ports to reproduce the original answer, and a later payment could quietly change a replayed result. Passing values makes "same inputs, same bytes out" true by construction.

_(Updated in Phase 2: Signals are added as an input.)_
