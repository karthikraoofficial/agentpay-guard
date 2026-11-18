# A Postgres major upgrade is a dump and restore into a new volume

Postgres 18 can't open a Postgres 16 data directory. The official 18 image also moved its data: `PGDATA` is `/var/lib/postgresql/18/docker` inside a volume at `/var/lib/postgresql`. On a 16 volume mounted at the old `/var/lib/postgresql/data` it refuses to start. So bumping the image tag, which is all Dependabot proposes, leaves every existing stack without a database. CI wouldn't notice, because its tests and the `deploy` smoke test start on empty databases.

So a major upgrade (#196, 16.6 to 18.7) works like this:
- **A new volume.** The Compose file names a new volume for the new major (`pgdata18`), mounted where the new image expects it.
- **A script does the move.** `infra/pg-upgrade.sh` (`--prod` on the VM) stops the stack, dumps the `agentpay` database from the old volume with the old image, and restores it into the new one. It then compares the row count of every table and fails if any differ.
- **The old copy stays.** The old volume and the dump are kept until someone has checked the new database, and are removed by hand. Rolling back is checking out the old Compose file and starting the stack again.

After the move, `api.admin verify`, `verify-pack` and `replay` give the same output as before it. A Decision replays byte for byte, because the restore copies the stored bytes and the Audit chain hashes them, not anything Postgres-specific.

We considered `pg_upgrade`, which is faster but needs both majors' binaries in one container (the image ships one). It also changes the old data directory in place with `--link`, so the rollback copy would go. The databases are small (a seeded local one dumped to 74 KB), so a dump takes seconds.

We also considered starting the hosted demo on an empty 18 database and recreating its Demo Tenant, which ADR-0021 allows. That would lose the demo's Audit chain and Disputes and change its keys, and every local stack would still need moving. The script costs little more and serves both.

A test keeps CI's Postgres image the same as Compose's, so the tests always run on the major that the stack runs.
