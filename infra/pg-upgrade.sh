#!/usr/bin/env bash
# Move this machine's Compose stack from Postgres 16 to 18 by dump and restore (ADR-0022).
#
#   bash infra/pg-upgrade.sh          # a local dev stack
#   bash infra/pg-upgrade.sh --prod   # the hosted demo, on the VM
#
# Run it after pulling the Compose file that names postgres:18.7 on the pgdata18 volume.
# It stops the stack, dumps the agentpay database from the 16 volume (pgdata), restores it
# into a new 18 volume (pgdata18), and checks every table has the same number of rows.
# It never deletes pgdata or the dump: they are the rollback. Start the stack again with
# `compose up -d` afterwards; the prod `migrate` service runs `alembic upgrade head`.
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1  # Git Bash on Windows: keep container paths as written

compose=(docker compose --env-file .env -f infra/docker-compose.yml)
if [[ "${1:-}" == "--prod" ]]; then
  compose+=(-f infra/docker-compose.prod.yml)
elif [[ -n "${1:-}" ]]; then
  echo "usage: bash infra/pg-upgrade.sh [--prod]" >&2
  exit 2
fi

old_image=${AGENTPAY_PG_OLD_IMAGE:-postgres:16.6}
project=$("${compose[@]}" config | sed -n 's/^name: //p')
old_volume="${project}_pgdata"
new_volume="${project}_pgdata18"
dump_dir=${AGENTPAY_PG_DUMP_DIR:-"$HOME/agentpay-pg-upgrade-$(date -u +%Y%m%dT%H%M%SZ)"}
old="${project}-pg-upgrade-16"
new="${project}-pg-upgrade-18"

say() { printf '\n== %s\n' "$*"; }
die() { printf 'pg-upgrade: %s\n' "$*" >&2; exit 1; }

docker volume inspect "$old_volume" >/dev/null 2>&1 || die "no $old_volume volume: nothing to move"
if docker volume inspect "$new_volume" >/dev/null 2>&1; then
  die "$new_volume already exists: the move has run before. Remove it by hand to run again."
fi

cleanup() { docker rm -f "$old" "$new" >/dev/null 2>&1 || true; }
trap cleanup EXIT

ready() {  # over TCP, so the entrypoint's socket-only first-start server doesn't count
  for _ in $(seq 1 60); do
    docker exec "$1" pg_isready -q -h 127.0.0.1 -U agentpay -d agentpay && return 0
    sleep 2
  done
  docker logs --tail 20 "$1" >&2
  die "$1 didn't become ready"
}

# Rows per table, one "table count" line each, sorted.
count_sql="select table_name || ' ' || (xpath('/row/c/text()', query_to_xml(format(
  'select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text
  from information_schema.tables
  where table_schema = 'public' and table_type = 'BASE TABLE' order by table_name"
counts() { docker exec "$1" psql -qAt -U agentpay -d agentpay -c "$count_sql"; }

say "Stopping the stack, so nothing writes during the move"
"${compose[@]}" stop
# Two servers on one data directory corrupt it.
if users=$(docker ps -q --filter "volume=$old_volume") && [[ -n "$users" ]]; then
  die "a running container still uses $old_volume ($(docker ps --filter "volume=$old_volume" --format '{{.Names}}' | tr '\n' ' ')); stop it first"
fi

say "Dumping agentpay from $old_volume ($old_image) into $dump_dir"
mkdir -p "$dump_dir"
docker run -d --name "$old" -v "$old_volume:/var/lib/postgresql/data" "$old_image" >/dev/null
ready "$old"
docker exec "$old" psql -qAt -U agentpay -d agentpay -c 'show server_version'
docker exec "$old" pg_dump -U agentpay -d agentpay -Fc > "$dump_dir/agentpay.dump"
docker exec "$old" pg_dumpall -U agentpay --globals-only > "$dump_dir/globals.sql"
counts "$old" > "$dump_dir/rows-before.txt"
docker stop "$old" >/dev/null
echo "$(wc -l < "$dump_dir/rows-before.txt") tables, dump $(wc -c < "$dump_dir/agentpay.dump") bytes"

say "Restoring into a new $new_volume"
# `compose run` creates the volume and the agentpay role and database from the Compose
# file's own settings (the prod password included), and publishes no port.
"${compose[@]}" run -d --no-deps --name "$new" postgres >/dev/null
ready "$new"
docker exec "$new" psql -qAt -U agentpay -d agentpay -c 'show server_version'
docker exec -i "$new" pg_restore -U agentpay -d agentpay --single-transaction --exit-on-error \
  < "$dump_dir/agentpay.dump"
docker exec "$new" vacuumdb -q -U agentpay -d agentpay --analyze-only
counts "$new" > "$dump_dir/rows-after.txt"
docker stop "$new" >/dev/null

say "Comparing rows per table"
if ! diff "$dump_dir/rows-before.txt" "$dump_dir/rows-after.txt"; then
  die "row counts differ (above). The stack is stopped; $old_volume is untouched."
fi
echo "all $(wc -l < "$dump_dir/rows-after.txt") tables match"

cat <<EOF

Moved. Next: ${compose[*]} up -d
Kept for rollback: the $old_volume volume and $dump_dir.
Rollback: check out infra/docker-compose.yml from before #196 (postgres:16.6 on
pgdata:/var/lib/postgresql/data), then run the same \`up -d\`.
EOF
