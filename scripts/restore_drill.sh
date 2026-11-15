#!/usr/bin/env bash
# Restore the newest off-site backup somewhere harmless, and prove it works.
#
# backup.sh proves that a dump arrived off-site with the right size. That is
# not the same as proving it restores: a dump can be complete and still fail
# on an extension the new server lacks, and an archive of originals can be
# intact and not hold the files the database points at. The only test of a
# backup is a restore, and restore.sh overwrites production, so it is not
# something anybody runs to find out. This is.
#
# What it does, touching nothing live:
#   1. takes the newest backup from BACKUP_REMOTE, and fails if it is stale,
#      which also catches a nightly backup that silently stopped running;
#   2. checks it against its own SHA256SUMS;
#   3. restores the dump into a throwaway Postgres, same image as production,
#      no network, with every error fatal;
#   4. checks the restored corpus: rows are there, a vector search runs, and
#      every original the database points at is in the archive with the
#      checksum the database recorded for it;
#   5. removes the throwaway container and every file it downloaded, because
#      those are client documents.
#
# A failure reaches the alert chat, and the next successful drill clears it.
#
# Usage: scripts/restore_drill.sh [deploy_root]
# Cron:  weekly, installed by infra/ansible/provision.yml
set -Eeuo pipefail

deploy_root="${1:-/opt/demo-firm-agent-v2}"
project="${COMPOSE_PROJECT:-demo-firm-v2}"
work_root="${DRILL_WORK_ROOT:-/var/backups/demo-firm-drill}"
# The backup runs nightly, so a newest copy older than a day and a half means
# at least one night did not arrive.
max_age_hours="${DRILL_MAX_AGE_HOURS:-36}"
drill_container="demo-firm-restore-drill"

log() { echo "[$(date -Is)] $*"; }
die() { echo "[$(date -Is)] FATAL: $*" >&2; exit 1; }

# shellcheck source=scripts/alert.sh
source "$(dirname "$0")/alert.sh"

work=""
cleanup() {
  local status=$?
  docker rm --force "$drill_container" >/dev/null 2>&1 || true
  [[ -n "$work" ]] && rm -rf "$work"
  if (( status != 0 )); then
    # Until the next weekly drill, which clears it or renews it.
    raise_alert RestoreDrillFailed 170 "The weekly restore drill failed" \
      "The newest off-site backup could not be restored and checked (status $status). Read /var/log/demo-firm-restore-drill.log on the VPS. Until this passes, nobody knows whether the backups can bring the firm's data back."
  fi
}
trap cleanup EXIT

cd "$deploy_root" || die "$deploy_root does not exist"
[[ -f .env ]] || die "$deploy_root/.env is missing"
# shellcheck disable=SC1091
set -a; source .env; set +a

[[ -n "${BACKUP_REMOTE:-}" ]] || die "BACKUP_REMOTE is not set, so there is no off-site copy to drill"
[[ "$BACKUP_REMOTE" != *@*:* ]] \
  || die "the drill reads rclone remotes only; $BACKUP_REMOTE is an scp target"
command -v rclone >/dev/null || die "rclone is not installed"

# ── The newest backup ────────────────────────────────────────────────────────
# Captured and then searched, never piped into a consumer that exits early:
# under pipefail that reports 141 on a listing that worked.
listing="$(rclone lsf --dirs-only "$BACKUP_REMOTE")" || die "cannot list $BACKUP_REMOTE"
stamp="$(grep -E '^[0-9]{8}T[0-9]{6}Z/$' <<<"$listing" | sort | tail -n 1)" \
  || die "no backup directory in $BACKUP_REMOTE"
stamp="${stamp%/}"

iso="${stamp:0:4}-${stamp:4:2}-${stamp:6:2}T${stamp:9:2}:${stamp:11:2}:${stamp:13:2}Z"
age_hours=$(( ($(date -u +%s) - $(date -u -d "$iso" +%s)) / 3600 ))
(( age_hours <= max_age_hours )) \
  || die "the newest off-site backup, $stamp, is ${age_hours}h old: the nightly backup has stopped arriving"
log "Drilling $BACKUP_REMOTE/$stamp (${age_hours}h old)"

mkdir -p "$work_root"
chmod 700 "$work_root"
work="$(mktemp -d "$work_root/drill.XXXXXX")"

rclone copy "$BACKUP_REMOTE/$stamp" "$work/backup" || die "could not download $stamp"
[[ -f "$work/backup/SHA256SUMS" ]] || die "$stamp has no SHA256SUMS"
( cd "$work/backup" && sha256sum --check --quiet SHA256SUMS ) \
  || die "$stamp does not match its own checksums"
log "Checksums match"

# ── Restore into a throwaway Postgres ────────────────────────────────────────
# The live container's image, so a drill that passes says something about a
# restore onto the server the firm would actually rebuild.
live_postgres="$(docker ps --quiet \
  --filter "label=com.docker.compose.project=$project" \
  --filter "label=com.docker.compose.service=postgres")"
image="pgvector/pgvector:pg16"
[[ -n "$live_postgres" ]] \
  && image="$(docker inspect --format '{{.Config.Image}}' "${live_postgres%%$'\n'*}")"

docker rm --force "$drill_container" >/dev/null 2>&1 || true
# No network: nothing restored here can reach, or be reached from, anywhere.
# A memory ceiling, because this shares a small host with production.
docker run --detach --name "$drill_container" --network none --memory 512m \
  -e POSTGRES_USER=drill -e POSTGRES_DB=drill \
  -e POSTGRES_PASSWORD="$(head -c 24 /dev/urandom | base64 | tr -d '/+=')" \
  "$image" >/dev/null

# Over TCP on purpose: the image's first start runs a temporary server on the
# socket only, and that one is about to be stopped. TCP answers only once the
# real server is up.
for _ in {1..60}; do
  docker exec "$drill_container" pg_isready -q -h 127.0.0.1 -U drill && break
  sleep 1
done
docker exec "$drill_container" pg_isready -q -h 127.0.0.1 -U drill \
  || die "the throwaway Postgres did not start"

# One transaction and every error fatal: a restore that skips a table with a
# warning is exactly the kind that passes a drill and fails the real day.
# Ownership and grants are left out, because the roles they name exist only
# on the live server.
docker exec -i "$drill_container" pg_restore --single-transaction --exit-on-error \
  --no-owner --no-privileges -U drill -d drill < "$work/backup/postgres.dump" \
  || die "pg_restore failed"
log "Database restored"

sql() {
  docker exec "$drill_container" psql -X -At -v ON_ERROR_STOP=1 -U drill -d drill -c "$1"
}

staff="$(sql "SELECT count(*) FROM staff")"
documents="$(sql "SELECT count(*) FROM documents")"
chunks="$(sql "SELECT count(*) FROM document_chunks")"
(( staff > 0 )) || die "the restored database has no staff"
(( documents > 0 )) || die "the restored database has no documents"
(( chunks > 0 )) || die "the restored database has no chunks"

# The restore could succeed with the vector type present and its index
# unusable; this is the query the assistant actually runs.
nearest="$(sql "SELECT count(*) FROM (SELECT id FROM document_chunks
  WHERE embedding IS NOT NULL
  ORDER BY embedding <=> (SELECT embedding FROM document_chunks
                          WHERE embedding IS NOT NULL LIMIT 1)
  LIMIT 5) AS hits")"
(( nearest > 0 )) || die "a vector search on the restored corpus returned nothing"

# ── Originals ────────────────────────────────────────────────────────────────
# A dump that restores without the files it points at is the backup that
# looks fine until somebody asks for /arquivo. Every original the database
# names must be in the archive, byte for byte.
[[ -f "$work/backup/documents.tar.gz" ]] || die "$stamp has no documents.tar.gz"
mkdir "$work/documents"
tar xzf "$work/backup/documents.tar.gz" -C "$work/documents" \
  || die "documents.tar.gz does not extract"

# Captured first: a query failing inside a process substitution is invisible
# to set -e, and the loop would then check nothing and pass.
#
# An original with a wrapped key lives in the object store, encrypted, and is
# not in documents.tar.gz by design. It is checked in the **backup's** copy of
# the bucket, not in the live bucket: a drill that reads production proves the
# production object exists, which is not a recovery property and would pass on
# the exact night the bucket was lost.
#
# Its checksum cannot be compared there, because the backup holds ciphertext
# and the recorded checksum is of the plaintext. Presence is what this can
# assert; that the bytes decrypt is asserted by the encryption tests.
recorded="$(sql "SELECT source_storage_key, source_sha256,
                        CASE WHEN source_wrapped_key IS NULL THEN 'volume' ELSE 'bucket' END
                 FROM documents WHERE source_storage_key IS NOT NULL")"
originals=0
in_bucket=0
objects_backup="$BACKUP_REMOTE/objects"
while IFS='|' read -r key expected where; do
  [[ -n "$key" ]] || continue
  # The key comes from a database, and this runs as root: a key that is not
  # the shape storage_key() produces never becomes a path.
  [[ "$key" =~ ^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}(\.[a-z0-9]{1,10})?$ ]] \
    || die "a document has an unexpected storage key: $key"
  if [[ "$where" == bucket ]]; then
    found="$(rclone lsf "$objects_backup/$key" 2>/dev/null)" || true
    [[ "$found" == "${key##*/}" ]] \
      || die "the original $key is not in $objects_backup. The backup carries the
wrapped key that opens this document but not the document; check that
backup.sh copied the bucket."
    in_bucket=$(( in_bucket + 1 ))
    continue
  fi
  [[ -f "$work/documents/$key" ]] || die "the original $key is not in the archive"
  actual="$(sha256sum "$work/documents/$key")"
  [[ "${actual%% *}" == "$expected" ]] || die "the original $key does not match its recorded checksum"
  originals=$(( originals + 1 ))
done <<<"$recorded"

# ── Redis ────────────────────────────────────────────────────────────────────
if [[ -f "$work/backup/redis.tar.gz" ]]; then
  tar tzf "$work/backup/redis.tar.gz" >/dev/null || die "redis.tar.gz is not a readable archive"
fi

clear_alert RestoreDrillFailed "The weekly restore drill passed again"
log "Drill passed: $stamp restored with $staff staff, $documents documents, $chunks chunks; $originals originals match their checksums, $in_bucket backed up from the bucket"
