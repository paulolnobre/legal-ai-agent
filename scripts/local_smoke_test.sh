#!/usr/bin/env bash
# Everything the VPS would do to a release, done on this machine first.
#
# Four layers, cheapest first, stopping at the first failure:
#
#   1. ruff and pytest        — the same checks CI runs, no Docker needed
#   2. compose validation     — catches a broken override before it reaches the VPS
#   3. image build            — catches a broken Dockerfile or an unresolvable pin
#   4. the stack, for real    — Postgres with pgvector, Redis, the app; /ready
#                               proves the schema installed and both stores answer,
#                               and a synthetic Telegram update proves the webhook
#                               and the queue worker are wired to each other
#
# The stack runs under its own Compose project, so it never touches the volumes
# of a `docker compose up` you already have running. It is torn down with its
# volumes at the end, including on failure.
#
# Usage: scripts/local_smoke_test.sh [--keep] [--fast]
#   --keep  leave the stack running afterwards (inspect logs, Grafana, psql)
#   --fast  layers 1 and 2 only: no image build, no containers
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

# A project name of its own: the smoke stack gets its own containers, its own
# volumes and its own network, so nothing here can corrupt a development
# database that happens to be running.
smoke_project="demo-firm-smoke"
compose=(docker compose -p "$smoke_project" -f docker-compose.yml)

keep_running=false
fast_only=false
for argument in "$@"; do
  case "$argument" in
    --keep) keep_running=true ;;
    --fast) fast_only=true ;;
    *) echo "Unknown option: $argument" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m    ok\033[0m %s\n' "$1"; }
fail() { printf '\033[31m    FAIL\033[0m %s\n' "$1" >&2; }

teardown() {
  local exit_code=$?
  if [[ "$stack_started" == true ]]; then
    if [[ "$keep_running" == true && $exit_code -eq 0 ]]; then
      printf '\nStack left running as requested (project %s).\n' "$smoke_project"
      printf 'App http://127.0.0.1:8000  •  tear down with:\n  %s down -v\n' "${compose[*]}"
    else
      step "Tearing down the smoke stack"
      "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
      ok "containers and volumes removed"
    fi
  fi
  if [[ $exit_code -ne 0 ]]; then
    printf '\n\033[31mSmoke test failed. Do not deploy this revision.\033[0m\n' >&2
  fi
  return $exit_code
}
stack_started=false
trap teardown EXIT

printf '\033[1mLocal smoke test\033[0m — branch %s, commit %s\n' \
  "$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')" \
  "$(git rev-parse --short HEAD 2>/dev/null || echo '?')"

# ── 1. The checks CI runs ────────────────────────────────────────────────────

step "Lint and tests"
if ! command -v ruff >/dev/null || ! command -v pytest >/dev/null; then
  fail "ruff and pytest must be on PATH — activate the virtualenv first"
  exit 1
fi
ruff check .
ok "ruff"
pytest -q
ok "pytest"

# ── 2. Compose files ─────────────────────────────────────────────────────────

step "Compose configuration"
[[ -f .env ]] || { fail ".env is missing — copy .env.example and fill it in"; exit 1; }
docker compose -f docker-compose.yml config --quiet
ok "docker-compose.yml"
docker compose -f docker-compose.yml --profile observability config --quiet
ok "observability profile"
# The production override needs the external Traefik network, which does not
# exist here; validating it locally would only prove that Traefik is missing.
ok "production override skipped (needs the VPS Traefik network)"

if [[ "$fast_only" == true ]]; then
  printf '\n\033[32mFast checks passed.\033[0m Run without --fast to exercise the stack.\n'
  exit 0
fi

# ── 3. Image ─────────────────────────────────────────────────────────────────

step "Building the application image"
"${compose[@]}" build legal-bot
ok "image builds"

# ── 4. The running stack ─────────────────────────────────────────────────────

step "Starting Postgres, Redis and the application"
stack_started=true
"${compose[@]}" up -d --wait --wait-timeout 120 2>/dev/null || {
  # --wait fails on the first unhealthy service; the logs say which one.
  fail "the stack did not become healthy"
  "${compose[@]}" ps
  "${compose[@]}" logs --tail=50 legal-bot >&2
  exit 1
}
ok "containers healthy"

# Published on loopback by docker-compose.yml, so this is the same port the
# reverse proxy would talk to on the VPS.
app="http://127.0.0.1:8000"

step "Health and readiness"
curl --fail --silent --show-error --max-time 10 "$app/health" >/dev/null
ok "/health"
# /ready touches both stores: green here means the pgvector schema installed and
# Redis accepted a command.
curl --fail --silent --show-error --max-time 10 "$app/ready" >/dev/null
ok "/ready — Postgres and Redis answered"

step "Delivering a synthetic Telegram update"
# The update is shaped like a real one from a private chat. It is not from an
# authorized chat_id, so the assistant answers with the access-request path:
# what is proven here is that the webhook accepts, enqueues, and the worker
# consumes — the wiring, not the legal reasoning.
secret="$(sed -n 's/^TELEGRAM_WEBHOOK_SECRET=//p' .env | tr -d '"'"'"'[:space:]')"
header=()
[[ -n "$secret" ]] && header=(-H "X-Telegram-Bot-Api-Secret-Token: $secret")

update_id=$(( RANDOM + 900000 ))
response=$(curl --fail --silent --show-error --max-time 15 \
  -X POST "$app/telegram/webhook" \
  -H "Content-Type: application/json" \
  "${header[@]}" \
  -d "{\"update_id\": $update_id,
       \"message\": {\"chat\": {\"id\": 999000111, \"type\": \"private\"},
                     \"from\": {\"id\": 999000111, \"first_name\": \"Smoke\"},
                     \"text\": \"/ajuda\"}}")

case "$response" in
  *'"ok":true'*) ok "webhook accepted the update: $response" ;;
  *) fail "unexpected webhook response: $response"; exit 1 ;;
esac

step "Confirming the worker consumed it"
consumed=false
for _ in {1..15}; do
  # An update that is still queued or reserved has not been handled yet.
  depth=$("${compose[@]}" exec -T redis redis-cli \
    eval "return redis.call('LLEN', KEYS[1]) + redis.call('LLEN', KEYS[2])" \
    2 legal:queue:active legal:queue:processing 2>/dev/null | tr -d '[:space:]')
  if [[ "$depth" == "0" ]]; then
    consumed=true
    break
  fi
  sleep 1
done

if [[ "$consumed" == true ]]; then
  ok "queue drained — the worker picked the update up"
else
  fail "the update stayed in the queue; the worker is not consuming"
  "${compose[@]}" logs --tail=40 legal-bot >&2
  exit 1
fi

# An unhandled exception in the worker still drains the queue through the retry
# path, so the log is checked for the traceback that would hide behind it.
# The log is captured before it is searched. Piping it into `grep -q` under
# `set -o pipefail` inverts this check exactly when it matters: grep finds the
# traceback, exits, compose takes SIGPIPE, the pipeline reports 141, and the
# condition reads as "no traceback found". A test that passes because it failed
# is worse than no test.
app_log="$("${compose[@]}" logs legal-bot 2>&1)"
if grep -q "Traceback (most recent call last)" <<<"$app_log"; then
  fail "the application logged a traceback"
  "${compose[@]}" logs --tail=60 legal-bot >&2
  exit 1
fi
ok "no tracebacks in the application log"

printf '\n\033[32mSmoke test passed.\033[0m This revision behaves on a clean stack.\n'
