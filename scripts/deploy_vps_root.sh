#!/usr/bin/env bash
# Root-owned deployment entrypoint invoked through the restricted deploy sudo rule.
set -Eeuo pipefail

deploy_root="/opt/demo-firm-agent-v2"
runtime_config="/etc/demo-firm-agent/runtime-compose.yml"
compose=(
  docker compose
  -p demo-firm-v2
  --env-file "$deploy_root/.env"
  -f "$runtime_config"
)

cd "$deploy_root"

if [[ ! -f .env ]]; then
  echo "Refusing deployment: $deploy_root/.env is missing." >&2
  exit 1
fi

if [[ ! -f "$runtime_config" ]]; then
  echo "Refusing deployment: $runtime_config is missing." >&2
  exit 1
fi

runtime_owner=$(stat -c '%U' -- "$runtime_config")
runtime_mode=$((8#$(stat -c '%a' -- "$runtime_config")))
if [[ "$runtime_owner" != "root" ]] || ((runtime_mode & 18)); then
  echo "Refusing deployment: $runtime_config must exist, be root-owned, and not be writable by deploy." >&2
  exit 1
fi

alertmanager_config="/etc/demo-firm-agent/alertmanager.yml"
services=(legal-bot prometheus grafana loki alloy redis-exporter)

# Alertmanager's configuration holds a bot token, so it is rendered by
# scripts/install_alertmanager_config.sh rather than synchronized with the
# source. Starting it without that file would make Docker create a directory
# where the file should be, and Alertmanager would crash-loop against it. A
# deploy that leaves alerting off with a clear message beats one that leaves it
# broken quietly.
if [[ -f "$alertmanager_config" ]]; then
  services+=(alertmanager)
else
  echo "Note: $alertmanager_config is missing, so alerting stays off." >&2
  echo "      Run scripts/install_alertmanager_config.sh as root to enable it." >&2
fi

"${compose[@]}" config --quiet
"${compose[@]}" --profile observability up -d --build "${services[@]}"

for attempt in {1..15}; do
  if curl --fail --silent --show-error http://127.0.0.1:8000/ready >/dev/null; then
    "${compose[@]}" ps legal-bot
    exit 0
  fi
  sleep 2
done

"${compose[@]}" logs --tail=100 legal-bot >&2
echo "Deployment failed: legal-bot did not become ready." >&2
exit 1
