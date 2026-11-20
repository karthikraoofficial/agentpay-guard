# Slow decisions

`POST /v1/decisions` is slower than its budget: p95 under 150 ms, the budget the `-m perf` tests hold the code to. A Decision calls neither the rail nor a model (non-negotiable 1), so slowness here comes from our own stack: Postgres, the VM, or the API process.

## How you'd notice

- **The alert (ADR-0026).** On the hosted demo, Grafana Cloud emails **Decisions: p95 latency over budget** when the p95 of `agentpay.decision.duration` stays above 150 ms for 15 minutes. That's measured over every new Decision, from the request's start to its commit. Replays aren't counted.
- **Traces.** In Langfuse, `decision` spans are slow, and their `signals.compute` child spans show whether the time went on the Signals.

## What already happens

Nothing slows down on purpose, and nothing is refused: each Decision still answers, only later. An Agent waiting on it may time out and retry. That's safe, since a retry with the same Idempotency-Key replays the stored response.

## What to do

1. **See whether the VM is short of CPU or memory:**

   ```bash
   docker stats --no-stream
   ```

2. **See whether Postgres is busy:**

   ```bash
   psql -c "select state, wait_event_type, count(*) from pg_stat_activity
            where datname = 'agentpay' group by 1, 2 order by 3 desc"
   ```

   Many `active` rows, or a `Lock` wait, point to the database.
3. **Look for a cause in the API log:**

   ```bash
   $C logs --since 1h api | grep -E "ERROR|Traceback|timeout" | tail -20
   ```

4. **Restart the API if it's the process:** `$C restart api`. Decisions in flight get an error and are retried by their Agents with the same Idempotency-Key.
5. **Don't** turn off Signals or change a Policy pack to make Decisions faster. Packs are immutable, and Decisions must replay from their Audit record.

## When it's over

- The alert resolves: p95 back under 150 ms over 15 minutes.
- `decision` spans in Langfuse are back to their usual length.
