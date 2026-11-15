#!/usr/bin/env bash
# Restore a backup produced by scripts/backup.sh.
#
# A backup that was never restored is a guess, so this script is also the drill:
# run it against a scratch environment every quarter and confirm the corpus
# answers afterwards.
#
# Usage: scripts/restore.sh <backup_dir> [deploy_root] [--database-only]
set -Eeuo pipefail

backup_dir="${1:?Usage: restore.sh <backup_dir> [deploy_root] [--database-only]}"
deploy_root="${2:-/opt/demo-firm-agent-v2}"
database_only="${3:-}"
project="${COMPOSE_PROJECT:-demo-firm-v2}"

log() { echo "[$(date -Is)] $*"; }
die() { echo "[$(date -Is)] FATAL: $*" >&2; exit 1; }

[[ -f "$backup_dir/postgres.dump" ]] || die "no postgres.dump in $backup_dir"

cd "$deploy_root" || die "$deploy_root does not exist"
# shellcheck disable=SC1091
set -a; source .env; set +a
db_name="${POSTGRES_DB:-${DB_NAME:-demo_firm}}"
db_user="${POSTGRES_USER:-${DB_USER:-demo_firm_agent}}"

# The listing is captured and then trimmed, rather than piped into `head`.
# Under `set -o pipefail`, head exits at the first line, docker takes SIGPIPE,
# and the pipeline reports 141: a failure that never happened.
container_of() {
  local ids
  ids="$(docker ps --quiet --all \
    --filter "label=com.docker.compose.project=$project" \
    --filter "label=com.docker.compose.service=$1")"
  printf '%s' "${ids%%$'\n'*}"
}

if [[ -f "$backup_dir/SHA256SUMS" ]]; then
  log "Verifying checksums"
  ( cd "$backup_dir" && sha256sum --check --quiet SHA256SUMS ) \
    || die "the backup is corrupt: checksums do not match"
fi

cat <<WARNING
About to overwrite the live data of $db_name in $deploy_root.
Everything not in this backup will be lost. Ctrl-C now to abort.
WARNING
read -r -p "Type RESTORE to continue: " confirmation
[[ "$confirmation" == "RESTORE" ]] || die "aborted"

log "Stopping the application so nothing writes during the restore"
docker stop "$(container_of legal-bot)" >/dev/null 2>&1 || true

# ── Postgres ─────────────────────────────────────────────────────────────────
postgres_container="$(container_of postgres)"
[[ -n "$postgres_container" ]] || die "the postgres container is not present"
docker start "$postgres_container" >/dev/null 2>&1 || true

log "Restoring the database"
docker exec -i "$postgres_container" \
  pg_restore --clean --if-exists --no-owner --username "$db_user" --dbname "$db_name" \
  < "$backup_dir/postgres.dump"

if [[ "$database_only" != "--database-only" ]]; then
  # ── Original documents ─────────────────────────────────────────────────────
  if [[ -f "$backup_dir/documents.tar.gz" ]]; then
    app_container="$(container_of legal-bot)"
    log "Restoring the original documents"
    docker start "$app_container" >/dev/null
    docker exec -i "$app_container" tar xzf - -C /data/documents \
      < "$backup_dir/documents.tar.gz"
    docker stop "$app_container" >/dev/null
  fi

  # ── Redis ──────────────────────────────────────────────────────────────────
  # Redis must not be running while its data directory is replaced: it would
  # overwrite the restored files with its own state on the next save.
  if [[ -f "$backup_dir/redis.tar.gz" ]]; then
    redis_container="$(container_of redis)"
    if [[ -n "$redis_container" ]]; then
      log "Restoring Redis (queue and caches)"
      docker stop "$redis_container" >/dev/null
      # A stopped container cannot run tar, so the volume is written through a
      # throwaway container that mounts the same volume.
      volume="$(docker inspect "$redis_container" \
        --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')"
      [[ -n "$volume" ]] || die "could not find the Redis data volume"
      docker run --rm -i -v "$volume:/data" alpine:3 \
        sh -c 'rm -rf /data/* /data/..?* 2>/dev/null; tar xzf - -C /data' \
        < "$backup_dir/redis.tar.gz"
      docker start "$redis_container" >/dev/null
    fi
  fi
fi

log "Starting the application"
docker start "$(container_of legal-bot)" >/dev/null

for _ in {1..15}; do
  if curl --fail --silent http://127.0.0.1:8000/ready >/dev/null; then
    log "Restore complete and the application is ready."
    exit 0
  fi
  sleep 2
done

die "restored, but the application did not become ready — check the logs"
