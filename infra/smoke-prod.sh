#!/usr/bin/env bash
# Bring the production stack up on this machine from .env.production.example, with
# AGENTPAY_API_HOST=localhost (Caddy's local CA), check /healthz answers through Caddy,
# then tear it all down. Runs as its own compose project, so a dev stack is left alone.
set -euo pipefail
cd "$(dirname "$0")/.."

env_file=.env.smoke
key=$(openssl rand -base64 32 | tr '+/' '-_')
sed -e 's/^AGENTPAY_API_HOST=.*/AGENTPAY_API_HOST=localhost/' \
    -e "s/^AGENTPAY_WEBHOOK_ENCRYPTION_KEY=.*/AGENTPAY_WEBHOOK_ENCRYPTION_KEY=$key/" \
    -e '/^AGENTPAY_LANGFUSE_/d' \
    .env.production.example > "$env_file"
export AGENTPAY_APP_ENV_FILE="../$env_file" AGENTPAY_HTTP_PORT=8080 AGENTPAY_HTTPS_PORT=8443
compose=(docker compose -p agentpay-smoke --env-file "$env_file"
         -f infra/docker-compose.yml -f infra/docker-compose.prod.yml)
cleanup() {
  "${compose[@]}" logs --no-color --tail 20 api worker migrate || true
  "${compose[@]}" down -v --remove-orphans || true
  rm -f "$env_file"
}
trap cleanup EXIT

"${compose[@]}" config --quiet
"${compose[@]}" up -d --build

for _ in $(seq 1 60); do
  if body=$(curl -fsSk https://localhost:8443/healthz); then
    echo "healthz through Caddy: $body"
    exit 0
  fi
  sleep 3
done
echo "healthz never answered through Caddy" >&2
exit 1
