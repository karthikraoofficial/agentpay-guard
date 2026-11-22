#!/usr/bin/env bash
# Bring the production stack up on this machine from .env.production.example, with
# AGENTPAY_API_HOST=localhost (Caddy's local CA), check the API's /healthz and the Demo
# runner's answer through Caddy, then tear it all down. Runs as its own compose project, so a dev stack is left alone.
set -euo pipefail
cd "$(dirname "$0")/.."

env_file=.env.smoke
key=$(openssl rand -base64 32 | tr '+/' '-_')
sed -e 's/^AGENTPAY_API_HOST=.*/AGENTPAY_API_HOST=localhost/' \
    -e "s/^AGENTPAY_WEBHOOK_ENCRYPTION_KEY=.*/AGENTPAY_WEBHOOK_ENCRYPTION_KEY=$key/" \
    -e '/^AGENTPAY_LANGFUSE_/d' \
    .env.production.example > "$env_file"
# The Demo runner starts only with test-format Demo Tenant keys; these reach no Tenant.
printf 'AGENTPAY_DEMO_AGENT_KEY=apg_test_smoke-agent
AGENTPAY_DEMO_TENANT_KEY=apg_test_smoke-tenant
' >> "$env_file"
export AGENTPAY_APP_ENV_FILE="../$env_file" AGENTPAY_HTTP_PORT=8080 AGENTPAY_HTTPS_PORT=8443
compose=(docker compose -p agentpay-smoke --env-file "$env_file"
         -f infra/docker-compose.yml -f infra/docker-compose.prod.yml)
cleanup() {
  "${compose[@]}" logs --no-color --tail 20 api worker migrate demo-runner || true
  "${compose[@]}" down -v --remove-orphans || true
  rm -f "$env_file"
}
trap cleanup EXIT

"${compose[@]}" config --quiet
"${compose[@]}" up -d --build

check() {
  local name="$1" url="$2" body
  for _ in $(seq 1 60); do
    if body=$(curl -fsSk "$url"); then
      echo "$name through Caddy: $body"
      return 0
    fi
    sleep 3
  done
  echo "$name never answered through Caddy" >&2
  return 1
}
check "API healthz" https://localhost:8443/healthz
check "Demo runner healthz" https://localhost:8443/demo-runner/healthz
