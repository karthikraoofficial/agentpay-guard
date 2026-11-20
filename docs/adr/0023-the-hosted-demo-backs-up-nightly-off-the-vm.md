# The hosted demo backs up nightly, off the VM, through a write-only link

ADR-0021 accepted that the hosted demo has no backups, since a Demo Tenant can be recreated. But recreating one loses its Audit chain, its Disputes and its keys, and Oracle may reclaim an idle Always Free VM. The dump and restore in ADR-0022 showed that a backup is cheap: the demo's database dumped to 112 KB and restored with its Audit chain, Evidence packs and replays intact.

So the hosted demo now backs up (#200). This amends the "Backups and failover" point of ADR-0021, which still holds for point-in-time recovery and replicas.

`infra/pg-backup.sh` runs from the VM's crontab:
- **Nightly dump at 02:30 UTC.** It runs `pg_dump -Fc` from the running Postgres, without stopping anything: pg_dump reads a consistent snapshot. A dump keeps its final name only after the whole archive reads back. The newest 14 are kept in `~/agentpay-backups`. A restore can lose up to a day of writes.
- **Restore check every Sunday at 03:00 UTC.** It restores the newest dump into a throwaway Postgres on the stack's network and runs `api.admin verify` for every Tenant. A dump that won't restore, or whose Audit chain is broken, fails the check. Without this, a backup could be broken for weeks before anyone found out.
- **A copy off the VM.** Each dump is uploaded to a private Object Storage bucket in the same tenancy, which deletes objects after 30 days. That's well inside the Always Free 20 GB.

The VM uploads with `curl` and a write-only pre-authenticated request (PAR). That link can write objects to the bucket but can't read, list or delete them. So the VM holds no OCI credentials, and a compromised VM can't read old backups. It could still overwrite one whose name it guesses, since names are timestamps. Bucket versioning would guard against that, and is left out to keep the lifecycle rule simple. The link is a secret: it's kept in the VM's `.env` and never printed. A PAR must expire, so it is made for a year, and the nightly log warns 30 days before it runs out. Re-running the wizard's backups stage replaces it. The bucket, its lifecycle rule (with the policy Object Storage needs to delete) and the PAR are made by `infra/oci-create-backup-bucket.sh` with the OCI CLI on the operator's machine, like the VM itself.

Failures are only logged (`~/agentpay-backups/backup.log` and `check.log`). A dead-man's-switch service that emails on a missed night was considered, and left out to avoid a third-party account for a demo. _(Amended by ADR-0026: each job now pings a Healthchecks.io check, which emails on a failure or a missed run.)_

A dump holds no card numbers, PINs or raw credentials (non-negotiable 7). API keys are stored hashed, and Tenant webhook secrets are encrypted with a key that is only in `.env` (ADR-0015), so a leaked dump doesn't hand over webhook secrets.

We considered keeping the dumps on the VM only. That protects against a bad deploy or a mistaken delete, but not against losing the VM, which is the likelier loss for an Always Free instance. We also considered instance-principal auth with the OCI CLI on the VM. That would need a dynamic group and a policy that grants access to the bucket, and it would let the VM read and delete backups too.
