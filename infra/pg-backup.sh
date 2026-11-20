#!/usr/bin/env bash
# The hosted demo's Postgres backups (#200, ADR-0023). On the VM, from the repo root:
#
#   bash infra/pg-backup.sh dump          # nightly: dump, keep the newest 14, copy off the VM
#   bash infra/pg-backup.sh check         # weekly: restore the newest dump, verify every Tenant
#   bash infra/pg-backup.sh install-cron  # once: both jobs in this user's crontab (idempotent)
#
# Dumps go to ~/agentpay-backups (AGENTPAY_BACKUP_DIR). With AGENTPAY_BACKUP_PAR_URL set in
# .env, each is also PUT to Object Storage through that write-only link, which this script
# never prints. AGENTPAY_BACKUP_PAR_EXPIRES (YYYY-MM-DD) gets a warning 30 days ahead.
# AGENTPAY_BACKUP_HEARTBEAT_URL and AGENTPAY_RESTORE_CHECK_HEARTBEAT_URL (Healthchecks.io,
# #227, ADR-0026) get a ping when dump or check succeeds, and their /fail when it fails.
# Nothing here stops the stack: pg_dump reads a consistent snapshot while the API runs.
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1  # Git Bash on Windows: keep container paths as written

env_file=${AGENTPAY_BACKUP_ENV_FILE:-.env}
dir=${AGENTPAY_BACKUP_DIR:-"$HOME/agentpay-backups"}
keep=${AGENTPAY_BACKUP_KEEP:-14}
compose=(docker compose --env-file "$env_file" -f infra/docker-compose.yml -f infra/docker-compose.prod.yml)

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "FAILED: $*" >&2; exit 1; }

# The value of KEY in the env file, without the quotes the wizard writes; empty if unset.
setting() {
  local value
  value=$(sed -n "s/^$1=//p" "$env_file" 2>/dev/null | tail -n1)
  value=${value#\'}; value=${value%\'}; value=${value#\"}; value=${value%\"}
  printf '%s' "$value"
}

# heartbeat KEY [fail]: ping KEY's URL, or its /fail, when KEY is set. The request carries
# nothing (no body, no data from the database), and the URL is a secret so it's never
# printed: curl's own errors are dropped. A ping that fails only logs, since a good backup
# isn't made bad by it, and the missed ping alerts on its own.
heartbeat() {
  local url
  url=$(setting "$1")
  [[ -n "$url" ]] || return 0
  url=${url%/}${2:+/$2}
  curl -fsS --retry 3 --max-time 10 "$url" > /dev/null 2>&1     || log "WARNING: the $1 heartbeat ping failed"
}

# with_heartbeat KEY FUNCTION: run FUNCTION in a subshell under set -e (its EXIT trap
# cleans up there), then ping KEY with how it went and exit with its status. Not in an
# `if`: bash ignores set -e inside a condition.
with_heartbeat() {
  local status
  set +e
  (set -e; "$2")
  status=$?
  set -e
  if (( status == 0 )); then heartbeat "$1"; else heartbeat "$1" fail; fi
  exit "$status"
}

dump() {
  mkdir -p "$dir"
  local stamp final partial
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  final="$dir/agentpay-$stamp.dump"
  partial="$final.partial"
  # Filled in now: the trap runs after this function's locals are gone.
  # shellcheck disable=SC2064
  trap "rm -f $(printf '%q' "$partial")" EXIT
  "${compose[@]}" exec -T postgres pg_dump -U agentpay -d agentpay -Fc > "$partial" \
    || die "pg_dump failed; no dump written"
  # Read the whole archive back, so a truncated dump never gets a dump's name.
  "${compose[@]}" exec -T postgres pg_restore -l < "$partial" > /dev/null \
    || die "the dump doesn't read back; no dump written"
  mv "$partial" "$final"
  log "dump ok: $(basename "$final"), $(wc -c < "$final" | tr -d ' ') bytes"

  local old
  old=$(find "$dir" -maxdepth 1 -name 'agentpay-*.dump' | sort | head -n "-$keep")
  if [[ -n "$old" ]]; then
    while IFS= read -r file; do rm -f "$file"; done <<< "$old"  # names may hold spaces
    log "removed $(printf '%s\n' "$old" | wc -l | tr -d ' ') dump(s) beyond the newest $keep"
  fi

  local par expires
  par=$(setting AGENTPAY_BACKUP_PAR_URL)
  if [[ -z "$par" ]]; then
    log "no off-VM copy: AGENTPAY_BACKUP_PAR_URL isn't set"
    return
  fi
  expires=$(setting AGENTPAY_BACKUP_PAR_EXPIRES)
  if [[ -n "$expires" ]] && (( $(date -u -d "$expires" +%s) - $(date -u +%s) < 30 * 86400 )); then
    log "WARNING: the backup upload link expires on $expires; re-run the provision wizard's backups stage"
  fi
  # -sS without -v: curl's errors name the status, never the URL. Bash, not curl, opens
  # /dev/null: with MSYS_NO_PATHCONV a native Windows curl can't.
  if curl -fsS --retry 3 --max-time 300 -T "$final" "$par$(basename "$final")" > /dev/null; then
    log "uploaded $(basename "$final") off the VM"
  else
    die "upload failed: $(basename "$final") is kept on the VM only"
  fi
}

check() {
  local newest project name password image tenants url failed=0
  newest=$(find "$dir" -maxdepth 1 -name 'agentpay-*.dump' 2>/dev/null | sort | tail -n1)
  [[ -n "$newest" ]] || die "no dump in $dir to check"
  project=$("${compose[@]}" config | sed -n 's/^name: //p')
  image=$("${compose[@]}" config --images | grep -m1 '^postgres:')
  name="$project-backup-check"
  password=$(openssl rand -hex 16)
  # shellcheck disable=SC2064  # filled in now, as in dump
  trap "docker rm -f $(printf '%q' "$name") > /dev/null 2>&1 || true" EXIT

  # A throwaway server on the stack's network: no volume kept (--rm takes its anonymous
  # one), no published port.
  docker run -d --rm --name "$name" --network "${project}_default" \
    -e POSTGRES_USER=agentpay -e POSTGRES_PASSWORD="$password" -e POSTGRES_DB=agentpay \
    "$image" > /dev/null
  for _ in $(seq 1 60); do  # over TCP, so the entrypoint's first-start server doesn't count
    docker exec "$name" pg_isready -q -h 127.0.0.1 -U agentpay -d agentpay && break
    sleep 2
  done
  docker exec -i "$name" pg_restore -U agentpay -d agentpay --single-transaction --exit-on-error \
    < "$newest" || die "$(basename "$newest") doesn't restore"

  tenants=$(docker exec "$name" psql -qAt -U agentpay -d agentpay -c 'select tenant_id from tenants order by 1')
  [[ -n "$tenants" ]] || die "$(basename "$newest") restores with no Tenants"
  url="postgresql+psycopg://agentpay:$password@$name:5432/agentpay"
  for tenant in $tenants; do
    if ! "${compose[@]}" run --rm --no-deps -T -e AGENTPAY_DATABASE_URL="$url" \
      migrate python -m api.admin verify --tenant "$tenant"; then
      failed=$((failed + 1))
    fi
  done
  (( failed == 0 )) || die "$(basename "$newest"): $failed Tenant(s) failed verify"
  log "check ok: $(basename "$newest") restores, and verify is clean for $(wc -w <<< "$tenants" | tr -d ' ') Tenant(s)"
}

install_cron() {
  mkdir -p "$dir"
  local repo current tag="# agentpay-backup"
  repo=$(pwd)
  # Read before writing: `crontab -l | ... | crontab -` can race the read against the write.
  current=$(crontab -l 2>/dev/null || true)
  {
    [[ -z "$current" ]] || grep -v "$tag\$" <<< "$current" || true
    echo "30 2 * * * cd '$repo' && bash infra/pg-backup.sh dump >> '$dir/backup.log' 2>&1 $tag"
    echo "0 3 * * 0 cd '$repo' && bash infra/pg-backup.sh check >> '$dir/check.log' 2>&1 $tag"
  } | crontab -
  log "cron: nightly dump at 02:30 UTC, restore check Sundays at 03:00 UTC; logs in $dir"
}

case "${1:-}" in
  dump) with_heartbeat AGENTPAY_BACKUP_HEARTBEAT_URL dump ;;
  check) with_heartbeat AGENTPAY_RESTORE_CHECK_HEARTBEAT_URL check ;;
  install-cron) install_cron ;;
  *) echo "usage: bash infra/pg-backup.sh dump|check|install-cron" >&2; exit 2 ;;
esac
