#!/usr/bin/env bash
# Where the hosted demo's nightly dumps go off the VM (#200, ADR-0023), with the OCI CLI.
# Run by the provision wizard's backups stage; safe to run again (it reuses what exists).
#
#   bash infra/oci-create-backup-bucket.sh <env-file>     # e.g. .env.production
#
# In the tenancy's home region:
#   - bucket agentpay-guard-backups: private (no public access), Standard tier
#   - policy agentpay-guard-backup-lifecycle: lets Object Storage itself delete old objects
#   - a lifecycle rule on the bucket: delete agentpay-* objects after 30 days
#   - a write-only pre-authenticated request (AnyObjectWrite, listing denied), expiring in
#     a year. Its URL is written to <env-file> as AGENTPAY_BACKUP_PAR_URL, with its expiry
#     as AGENTPAY_BACKUP_PAR_EXPIRES, and is never printed. A link with more than 30 days
#     left is kept rather than replaced.
# No OCI credential goes to the VM: only that URL, which can add objects and nothing else.
set -uo pipefail
export SUPPRESS_LABEL_WARNING=True PYTHONWARNINGS=ignore OCI_CLI_AUTH="${OCI_CLI_AUTH:-security_token}"
ENV_FILE="${1:?usage: bash infra/oci-create-backup-bucket.sh <env-file>}"
BUCKET=agentpay-guard-backups
KEEP_DAYS=30
command -v oci >/dev/null || { echo "Install the OCI CLI first: uv tool install oci-cli" >&2; exit 2; }
if [[ "$OCI_CLI_AUTH" == security_token ]] && ! oci session validate </dev/null >/dev/null 2>&1; then
  read -r -p "  Oracle region id (e.g. ap-hyderabad-1): " REGION
  read -r -p "  Tenancy name (top right of the console): " TENANCY_NAME
  oci session authenticate --region "$REGION" --tenancy-name "$TENANCY_NAME" --profile-name DEFAULT || exit 1
fi
config() { sed -n "/^\[${OCI_CLI_PROFILE:-DEFAULT}\]/,/^\[/p" ~/.oci/config | grep -m1 "^$1" | cut -d= -f2 | tr -d ' '; }
T=$(config tenancy)
REGION=$(config region)
q() { oci "$@" </dev/null 2>/dev/null; }

# KEY='value' in ENV_FILE, replacing any line for KEY (the provision wizard's format).
put() {
  local tmp; tmp=$(mktemp)
  [[ -e "$ENV_FILE" ]] || (umask 077 && : > "$ENV_FILE")
  grep -vE "^$1=" "$ENV_FILE" > "$tmp" || true
  printf "%s='%s'\n" "$1" "$2" >> "$tmp"
  cat "$tmp" > "$ENV_FILE"
  rm -f "$tmp"
}
current() { sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null | tail -n1 | tr -d "'\""; }

if q os bucket get --bucket-name "$BUCKET" >/dev/null; then
  echo "  bucket $BUCKET exists"
else
  q os bucket create -c "$T" --name "$BUCKET" --public-access-type NoPublicAccess \
    --storage-tier Standard >/dev/null || { echo "couldn't create bucket $BUCKET" >&2; exit 1; }
  echo "  bucket $BUCKET created (private)"
fi

# Lifecycle rules run as the Object Storage service, which needs leave to delete.
POLICY=agentpay-guard-backup-lifecycle
if [[ -z "$(q iam policy list -c "$T" --name "$POLICY" --query 'data[0].id' --raw-output)" ]]; then
  q iam policy create -c "$T" --name "$POLICY" \
    --description "Lets Object Storage delete expired AgentPay Guard backups" \
    --statements "[\"Allow service objectstorage-$REGION to manage object-family in tenancy\"]" >/dev/null \
    || { echo "couldn't create policy $POLICY" >&2; exit 1; }
  echo "  policy $POLICY created"
fi
rule="[{\"name\":\"delete-after-$KEEP_DAYS-days\",\"action\":\"DELETE\",\"timeAmount\":$KEEP_DAYS,\"timeUnit\":\"DAYS\",\"isEnabled\":true,\"objectNameFilter\":{\"inclusionPrefixes\":[\"agentpay-\"]}}]"
# A new policy takes a little while to apply; until it does, the rule is refused.
for attempt in 1 2 3 4 5 6; do
  q os object-lifecycle-policy put --bucket-name "$BUCKET" --force --items "$rule" >/dev/null && break
  (( attempt < 6 )) || { echo "couldn't set the lifecycle rule (is the policy $POLICY there?)" >&2; exit 1; }
  echo "  waiting for the policy to apply…"
  sleep 10
done
echo "  lifecycle: agentpay-* objects deleted after $KEEP_DAYS days"

expires=$(current AGENTPAY_BACKUP_PAR_EXPIRES)
if [[ -n "$(current AGENTPAY_BACKUP_PAR_URL)" && -n "$expires" ]] \
   && (( $(date -u -d "$expires" +%s) - $(date -u +%s) > 30 * 86400 )); then
  echo "  keeping the upload link in $ENV_FILE (expires $expires)"
  exit 0
fi
expires=$(date -u -d '+1 year' +%Y-%m-%d)
uri=$(q os preauth-request create --bucket-name "$BUCKET" --name "agentpay-backup-upload-$(date -u +%Y%m%d)" \
  --access-type AnyObjectWrite --time-expires "${expires}T00:00:00Z" \
  --query 'data."access-uri"' --raw-output)
[[ "$uri" == /p/* ]] || { echo "couldn't create the upload link" >&2; exit 1; }
put AGENTPAY_BACKUP_PAR_URL "https://objectstorage.$REGION.oraclecloud.com$uri"
put AGENTPAY_BACKUP_PAR_EXPIRES "$expires"
echo "  write-only upload link written to $ENV_FILE (expires $expires; not shown)"
