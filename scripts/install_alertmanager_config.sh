#!/usr/bin/env bash
# Render the Alertmanager configuration from the deployed .env and install it
# as a root-owned file, next to runtime-compose.yml.
#
# Run as root on the VPS, once, and again whenever the token or the chat
# changes:
#
#   sudo /opt/demo-firm-agent-v2/scripts/install_alertmanager_config.sh
#
# Two things make this a script instead of a mounted file from the repository.
# Alertmanager does not expand environment variables in its configuration, so
# the token has to be written in. And a file holding a token that can send
# messages as the firm's bot must not sit in a directory the deploy account can
# write, which rules out everything under /opt/demo-firm-agent-v2.
set -Eeuo pipefail

deploy_root="/opt/demo-firm-agent-v2"
template="$deploy_root/infra/alertmanager/alertmanager.template.yml"
destination="/etc/demo-firm-agent/alertmanager.yml"
alertmanager_image="prom/alertmanager:v0.28.1"

# The official image runs as nobody. A root-owned 0600 file would be invisible
# to it, so ownership is handed to that uid and nothing else can read it.
container_uid=65534

if [[ $EUID -ne 0 ]]; then
  echo "Run this as root: the destination is outside the deploy account's reach." >&2
  exit 1
fi

if [[ ! -f "$template" ]]; then
  echo "Missing template: $template" >&2
  exit 1
fi

if [[ ! -f "$deploy_root/.env" ]]; then
  echo "Missing $deploy_root/.env" >&2
  exit 1
fi

# Read the two values without exporting the whole environment file, which holds
# every other secret this application has.
read_env() {
  local key="$1"
  sed -n "s/^${key}=//p" "$deploy_root/.env" | tail -n 1
}

ALERT_TELEGRAM_BOT_TOKEN="$(read_env ALERT_TELEGRAM_BOT_TOKEN)"
ALERT_TELEGRAM_CHAT_ID="$(read_env ALERT_TELEGRAM_CHAT_ID)"

# Falling back to the assistant's own bot is deliberate. Alertmanager talks to
# Telegram's servers, not to this application, so the token keeps working
# exactly when it matters most: while the bot process is down. The alert then
# arrives in the conversation the owner already watches.
if [[ -z "$ALERT_TELEGRAM_BOT_TOKEN" ]]; then
  ALERT_TELEGRAM_BOT_TOKEN="$(read_env LEGAL_TELEGRAM_BOT_TOKEN)"
fi

if [[ -z "$ALERT_TELEGRAM_BOT_TOKEN" ]]; then
  echo "Set ALERT_TELEGRAM_BOT_TOKEN or LEGAL_TELEGRAM_BOT_TOKEN in $deploy_root/.env" >&2
  exit 1
fi

if [[ -z "$ALERT_TELEGRAM_CHAT_ID" ]]; then
  echo "Set ALERT_TELEGRAM_CHAT_ID in $deploy_root/.env: the chat that receives alerts." >&2
  exit 1
fi

export ALERT_TELEGRAM_BOT_TOKEN ALERT_TELEGRAM_CHAT_ID

mkdir -p "$(dirname "$destination")"
staged="$(mktemp)"
trap 'rm -f "$staged"' EXIT
chmod 0600 "$staged"

envsubst '${ALERT_TELEGRAM_BOT_TOKEN} ${ALERT_TELEGRAM_CHAT_ID}' < "$template" > "$staged"

# Check the rendered file before it replaces a working one. An Alertmanager
# that refuses to start is an alerting system that fails silently, which is
# worse than none at all.
if command -v docker >/dev/null 2>&1; then
  # --user 0:0 because the staged file is 0600 and owned by root, and the
  # image runs as nobody: without it amtool answers "permission denied", set -e
  # aborts, and alerting can never be installed at all. The alternative, a
  # world-readable temporary file, would leave the bot token readable by every
  # account on the box for as long as the check runs.
  docker run --rm --user 0:0 --entrypoint amtool \
    -v "$staged:/tmp/alertmanager.yml:ro" \
    "$alertmanager_image" check-config /tmp/alertmanager.yml
fi

install -o "$container_uid" -g "$container_uid" -m 0400 "$staged" "$destination"
echo "Installed $destination"
echo "Now restart Alertmanager so it reads the new configuration:"
echo "  docker compose -p demo-firm-v2 --env-file $deploy_root/.env \\"
echo "    -f /etc/demo-firm-agent/runtime-compose.yml \\"
echo "    --profile observability up -d alertmanager"
