#!/usr/bin/env bash
# Create the hosted demo's VM on Oracle Cloud with the OCI CLI (ADR-0021).
# Run by stage 1 of infra/provision-wizard.sh; safe to run again (it reuses what exists).
#
#   bash infra/oci-create-vm.sh [--guardrails]
#
# Signs in through the browser (`oci session authenticate`) if there's no live session,
# then, in the home region:
#   - an SSH key pair at ~/.ssh/agentpay-oci (the private half never leaves this machine)
#   - VCN agentpay-guard-vcn: internet gateway, default route, a public subnet, and
#     ingress for 22, 80 and 443
#   - VM agentpay-guard: VM.Standard.A1.Flex, 2 OCPUs / 12 GB, Ubuntu 24.04 aarch64, public IP
# With --guardrails (for a Pay As You Go account) it first adds a tenancy quota that allows
# only the Always Free Ampere allowance and 200 GB of block storage, and a budget that
# emails on any actual or forecast spend.
#
# Free accounts often get "Out of host capacity"; it retries every 10 minutes (15 after a
# throttle) until the VM launches. The last line printed is PUBLIC_IP=<ip>.
set -uo pipefail
# A browser session by default; OCI_CLI_AUTH=api_key (with OCI_CLI_PROFILE) uses an API key instead.
export SUPPRESS_LABEL_WARNING=True PYTHONWARNINGS=ignore OCI_CLI_AUTH="${OCI_CLI_AUTH:-security_token}"

GUARDRAILS=false; [[ "${1:-}" == "--guardrails" ]] && GUARDRAILS=true
KEY="$HOME/.ssh/agentpay-oci"
OCPUS=2; MEM=12

command -v oci >/dev/null || { echo "Install the OCI CLI first: uv tool install oci-cli" >&2; exit 2; }
if [[ "$OCI_CLI_AUTH" == security_token ]] && ! oci session validate </dev/null >/dev/null 2>&1; then
  read -r -p "  Oracle region id (e.g. ap-hyderabad-1): " REGION
  read -r -p "  Tenancy name (top right of the console): " TENANCY_NAME
  oci session authenticate --region "$REGION" --tenancy-name "$TENANCY_NAME" --profile-name DEFAULT || exit 1
fi
T=$(sed -n "/^\[${OCI_CLI_PROFILE:-DEFAULT}\]/,/^\[/p" ~/.oci/config | grep -m1 '^tenancy' | cut -d= -f2 | tr -d ' ')
q() { oci "$@" </dev/null 2>/dev/null; }
id_of() { q "$@" --query 'data[0].id' --raw-output; }

if $GUARDRAILS; then
  if [[ -z "$(id_of limits quota list -c "$T" --name always-free-only)" ]]; then
    statements=$(mktemp)
    cat > "$statements" <<'EOF'
["zero compute-core quotas in tenancy",
 "zero compute-memory quotas in tenancy",
 "set compute-core quota standard-a1-core-count to 2 in tenancy",
 "set compute-core quota standard-a1-core-regional-count to 2 in tenancy",
 "set compute-memory quota standard-a1-memory-count to 12 in tenancy",
 "set compute-memory quota standard-a1-memory-regional-count to 12 in tenancy",
 "set block-storage quota total-storage-gb to 200 in tenancy"]
EOF
    q limits quota create -c "$T" --name always-free-only --statements "file://$statements" \
      --description "Caps compute and storage at the Always Free allowance" >/dev/null \
      && echo "  quota always-free-only created"
    rm -f "$statements"
  fi
  if ! q budgets budget budget list -c "$T" --query 'data[]."display-name"' | grep -q always-free-guard; then
    read -r -p "  Email for spend alerts: " EMAIL
    B=$(q budgets budget budget create -c "$T" --display-name always-free-guard --amount 1 \
      --reset-period MONTHLY --target-type COMPARTMENT --targets "[\"$T\"]" --query 'data.id' --raw-output)
    for type in ACTUAL FORECAST; do
      q budgets budget alert-rule create --budget-id "$B" --type $type --threshold 1 \
        --threshold-type PERCENTAGE --recipients "$EMAIL" \
        --message "Oracle Cloud has $type spend above zero; the demo should be Always Free only." >/dev/null
    done
    echo "  budget always-free-guard created, alerts to $EMAIL"
  fi
fi

[[ -f "$KEY" ]] || ssh-keygen -q -t ed25519 -N "" -C agentpay-guard -f "$KEY"

VCN=$(id_of network vcn list -c "$T" --display-name agentpay-guard-vcn)
if [[ -z "$VCN" ]]; then
  VCN=$(q network vcn create -c "$T" --cidr-blocks '["10.0.0.0/16"]' --display-name agentpay-guard-vcn \
    --dns-label agentpay --wait-for-state AVAILABLE --query 'data.id' --raw-output)
  IGW=$(q network internet-gateway create -c "$T" --vcn-id "$VCN" --is-enabled true \
    --display-name agentpay-guard-igw --wait-for-state AVAILABLE --query 'data.id' --raw-output)
  RT=$(q network vcn get --vcn-id "$VCN" --query 'data."default-route-table-id"' --raw-output)
  q network route-table update --rt-id "$RT" --force --route-rules \
    "[{\"destination\":\"0.0.0.0/0\",\"destinationType\":\"CIDR_BLOCK\",\"networkEntityId\":\"$IGW\"}]" >/dev/null
  SL=$(q network vcn get --vcn-id "$VCN" --query 'data."default-security-list-id"' --raw-output)
  rule() { echo "{\"source\":\"0.0.0.0/0\",\"protocol\":\"6\",\"tcpOptions\":{\"destinationPortRange\":{\"min\":$1,\"max\":$1}}}"; }
  q network security-list update --security-list-id "$SL" --force --ingress-security-rules \
    "[$(rule 22),$(rule 80),$(rule 443),{\"source\":\"0.0.0.0/0\",\"protocol\":\"1\",\"icmpOptions\":{\"type\":3,\"code\":4}}]" >/dev/null
  echo "  network agentpay-guard-vcn created (ports 22, 80, 443 open)"
fi
SUBNET=$(id_of network subnet list -c "$T" --vcn-id "$VCN" --display-name agentpay-guard-public)
[[ -n "$SUBNET" ]] || SUBNET=$(q network subnet create -c "$T" --vcn-id "$VCN" --cidr-block 10.0.0.0/24 \
  --display-name agentpay-guard-public --dns-label public --prohibit-public-ip-on-vnic false \
  --wait-for-state AVAILABLE --query 'data.id' --raw-output)

AD=$(q iam availability-domain list -c "$T" --query 'data[0].name' --raw-output)
IMAGE=$(q compute image list -c "$T" --operating-system "Canonical Ubuntu" --operating-system-version 24.04 \
  --shape VM.Standard.A1.Flex --sort-by TIMECREATED --limit 1 --query 'data[0].id' --raw-output)

INST=$(q compute instance list -c "$T" --display-name agentpay-guard \
  --query 'data[?"lifecycle-state"!=`TERMINATED` && "lifecycle-state"!=`TERMINATING`] | [0].id' --raw-output)
i=0; last_refresh=$(date +%s)
while [[ -z "$INST" || "$INST" == "null" ]]; do
  i=$((i + 1))
  # A CLI session lasts an hour; it can be refreshed for up to 24 hours.
  if [[ "$OCI_CLI_AUTH" == security_token ]] && (( $(date +%s) - last_refresh >= 1200 )); then
    oci session refresh </dev/null >/dev/null 2>&1 || { echo "Session expired: run this again" >&2; exit 3; }
    last_refresh=$(date +%s)
  fi
  out=$(oci compute instance launch --no-retry -c "$T" --availability-domain "$AD" \
    --display-name agentpay-guard --shape VM.Standard.A1.Flex \
    --shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEM}" --image-id "$IMAGE" \
    --subnet-id "$SUBNET" --assign-public-ip true --ssh-authorized-keys-file "$KEY.pub" </dev/null 2>&1)
  if echo "$out" | grep -qE '"lifecycle-state": "(PROVISIONING|RUNNING)"'; then
    INST=$(echo "$out" | grep -m1 '"id": "ocid1.instance' | cut -d'"' -f4)
    break
  fi
  if echo "$out" | grep -q TooManyRequests; then wait=900
  elif echo "$out" | grep -qi capacity; then wait=600
  else echo "$out" | grep -m1 '"message"' >&2; exit 1; fi
  echo "  $(date +%H:%M) attempt $i: $(echo "$out" | grep -m1 -oE '"message": "[^"]*"' | cut -d'"' -f4); retrying in $((wait / 60)) min"
  sleep "$wait"
done
q compute instance get --instance-id "$INST" --wait-for-state RUNNING --max-wait-seconds 600 >/dev/null
echo "  VM agentpay-guard is running"
echo "PUBLIC_IP=$(q compute instance list-vnics --instance-id "$INST" --query 'data[0]."public-ip"' --raw-output)"
