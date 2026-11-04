# The hosted demo runs on one Always Free VM, not Render and Temporal Cloud

The build plan put the API and worker on Render and Temporal on Temporal Cloud. That doesn't fit a demo meant to stay up for free. Render's free web services sleep when idle and have no free background worker, and the worker must run all the time: it hosts the consent and Dispute workflows, the outbox relay and the sweep. Temporal Cloud is paid after its trial. Managed Postgres free tiers expire or pause.

So the hosted demo is one Oracle Cloud Always Free ARM VM running the same Docker Compose stack as local dev, plus `infra/docker-compose.prod.yml`. That stack is Postgres with a volume, the Temporal dev server with its SQLite file on a volume, the OTel collector (sending traces to Langfuse), the API, the worker, a one-shot migration, and Caddy. Caddy is the only public service: it terminates HTTPS for a free `sslip.io` hostname with a Let's Encrypt certificate it renews itself. The web app is on Vercel Hobby and traces go to Langfuse Hobby. The only running cost is model calls, bounded by the Model spend cap (ADR-0020). One `Dockerfile` builds the API, the worker and the migration.

What we give up, knowingly for a demo:
- **Backups and failover.** Postgres has no point-in-time recovery or replica, and losing the VM loses the data. A Demo Tenant can be recreated.
- **A production Temporal.** The dev server keeps workflow history in one SQLite file. It survives restarts, but it isn't a cluster.
- **Reliability guarantees.** Oracle can reclaim an Always Free instance it judges idle, and the free tier has no SLA.
- **Scale.** There is one worker, which matches the existing "run one worker" limit.

None of this touches the domain code: the seams are unchanged. Managed Postgres is a new database URL. Temporal Cloud also needs its TLS or API-key settings, which the app doesn't have yet. Both are on the go-live checklist in docs/BUILD-PLAN.md.
