#!/usr/bin/env bash
# Nightly backup of everything the firm cannot rebuild:
#
#   postgres   the corpus, embeddings, staff and the audit log
#   documents  the original uploaded files — a dump without them restores an
#              index pointing at files that no longer exist
#   redis      the durable Telegram update queue (the caches in it are
#              disposable, the queue is not: it holds messages not yet answered)
#
# Containers are found by their Compose project labels rather than through a
# compose file, so this keeps working whichever file the stack was started with
# — the VPS runs a root-owned /etc/demo-firm-agent/runtime-compose.yml that
# this script cannot read.
#
# A backup that only exists on the machine being backed up is not a backup, so
# BACKUP_REMOTE is required. Set ALLOW_LOCAL_ONLY_BACKUP=true only to test.
#
# Usage: scripts/backup.sh [deploy_root] [backup_root]
# Cron:  installed by scripts/install_backup_cron.sh
set -Eeuo pipefail

deploy_root="${1:-/opt/demo-firm-agent-v2}"
backup_root="${2:-/var/backups/demo-firm}"
retention_days="${BACKUP_RETENTION_DAYS:-14}"
project="${COMPOSE_PROJECT:-demo-firm-v2}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
destination="$backup_root/$stamp"

log() { echo "[$(date -Is)] $*"; }
die() { echo "[$(date -Is)] FATAL: $*" >&2; exit 1; }

# A failed backup used to be a line in /var/log/demo-firm-backup.log and
# nothing else, which is how a backup goes missing for weeks. Any non-zero
# exit, from die or from anything set -e stops on, now reaches the firm's
# alert chat. It lasts until the next night's run, which clears or renews it.
# shellcheck source=scripts/alert.sh
source "$(dirname "$0")/alert.sh"
report_failure() {
  local status=$?
  (( status == 0 )) && return 0
  raise_alert BackupFailed 26 "The nightly backup failed" \
    "scripts/backup.sh exited with status $status. Read /var/log/demo-firm-backup.log on the VPS. Until a run succeeds, the newest copy is the one before tonight."
}
trap report_failure EXIT

cd "$deploy_root" || die "$deploy_root does not exist"
[[ -f .env ]] || die "$deploy_root/.env is missing"

# shellcheck disable=SC1091
set -a; source .env; set +a
db_name="${POSTGRES_DB:-${DB_NAME:-demo_firm}}"
db_user="${POSTGRES_USER:-${DB_USER:-demo_firm_agent}}"

if [[ -z "${BACKUP_REMOTE:-}" && "${ALLOW_LOCAL_ONLY_BACKUP:-false}" != "true" ]]; then
  die "BACKUP_REMOTE is not set. A copy that lives only on this VPS does not
survive losing this VPS. Set BACKUP_REMOTE in $deploy_root/.env (an rclone
remote such as 'mgc:demo-firm-backup', or an scp target such as
'user@host:/backups'), or set ALLOW_LOCAL_ONLY_BACKUP=true to override."
fi

# The container of one Compose service, found by label. Empty when not running.
#
# The listing is captured and then trimmed, rather than piped into `head`.
# Under `set -o pipefail`, head exits at the first line, docker takes SIGPIPE,
# and the pipeline reports 141: a failure that never happened, on a lookup that
# succeeded.
container_of() {
  local ids
  ids="$(docker ps --quiet \
    --filter "label=com.docker.compose.project=$project" \
    --filter "label=com.docker.compose.service=$1")"
  printf '%s' "${ids%%$'\n'*}"
}

postgres_container="$(container_of postgres)"
redis_container="$(container_of redis)"
app_container="$(container_of legal-bot)"
[[ -n "$postgres_container" ]] || die "the postgres container of project '$project' is not running"

# 0700: these files hold client documents and the whole corpus.
mkdir -p "$destination"
chmod 700 "$backup_root" "$destination"

# ── Postgres ─────────────────────────────────────────────────────────────────
log "Dumping database $db_name"
docker exec -i "$postgres_container" \
  pg_dump --format=custom --no-owner --username "$db_user" "$db_name" \
  > "$destination/postgres.dump"

# ── Original documents ───────────────────────────────────────────────────────
# Streamed out of the running container: no image build, no volume-name guess.
if [[ -n "$app_container" ]]; then
  log "Archiving the original-documents volume"
  docker exec "$app_container" tar czf - -C /data/documents . \
    > "$destination/documents.tar.gz" \
    || die "could not archive /data/documents"
else
  echo "WARNING: legal-bot is not running; original documents were NOT archived" >&2
fi

# ── Redis ────────────────────────────────────────────────────────────────────
# SAVE first so the RDB on disk is a consistent point in time, then take the
# whole /data directory: with appendonly on, Redis restores from the AOF, so the
# RDB alone would not be restorable.
if [[ -n "$redis_container" ]]; then
  log "Snapshotting Redis"
  docker exec "$redis_container" redis-cli SAVE >/dev/null \
    || echo "WARNING: redis SAVE failed; the archive may lag the live state" >&2
  docker exec "$redis_container" tar czf - -C /data . \
    > "$destination/redis.tar.gz" \
    || die "could not archive the Redis data directory"
else
  echo "WARNING: redis is not running; the update queue was NOT archived" >&2
fi

# ── Integrity ────────────────────────────────────────────────────────────────
log "Writing checksums"
( cd "$destination" && sha256sum ./* > SHA256SUMS )
chmod 600 "$destination"/*

# A backup nobody can read back is not a backup: fail loudly on an empty dump.
dump_size="$(stat -c%s "$destination/postgres.dump")"
if (( dump_size < 4096 )); then
  die "the database dump is suspiciously small ($dump_size bytes)"
fi

# ── Off-site ─────────────────────────────────────────────────────────────────
# Off-site retention defaults to the same window as local, but is independent:
# a firm keeping legal records longer can raise BACKUP_REMOTE_RETENTION_DAYS
# without also filling the VPS's own disk with old local copies.
remote_retention_days="${BACKUP_REMOTE_RETENTION_DAYS:-$retention_days}"
is_scp_remote=false
[[ "${BACKUP_REMOTE:-}" == *@*:* ]] && is_scp_remote=true

if [[ -n "${BACKUP_REMOTE:-}" ]]; then
  log "Copying off-site to $BACKUP_REMOTE"
  if [[ "$is_scp_remote" == true ]]; then
    scp -r "$destination" "$BACKUP_REMOTE/$stamp" \
      || die "the off-site copy failed; the local copy is at $destination"
  else
    command -v rclone >/dev/null || die "rclone is not installed but BACKUP_REMOTE looks like an rclone remote"
    rclone copy "$destination" "$BACKUP_REMOTE/$stamp" \
      || die "the off-site copy failed; the local copy is at $destination"
    # Prove it arrived, rather than trusting a zero exit code.
    #
    # The listing is captured before it is searched. Piping rclone into
    # `grep -q` under `set -o pipefail` fails whenever grep finds the match and
    # exits before rclone has finished writing: rclone takes SIGPIPE, the
    # pipeline reports 141, and a backup that arrived intact is declared
    # missing. That is what happened on 2026-09-09 at 02:10, two seconds after
    # a copy that had worked, and it is a race, so it had passed the night
    # before.
    remote_listing="$(rclone lsf "$BACKUP_REMOTE/$stamp")" \
      || die "the off-site copy cannot be listed back"
    grep -qx 'postgres\.dump' <<<"$remote_listing" \
      || die "the off-site copy reported success but postgres.dump is not there"

    # Presence is not arrival. A truncated upload leaves a file with the right
    # name, and the night that matters is the one where the dump is restored.
    remote_dump_size="$(rclone size --json "$BACKUP_REMOTE/$stamp/postgres.dump" \
      | sed -n 's/.*"bytes":[[:space:]]*\([0-9]*\).*/\1/p')"
    if [[ "$remote_dump_size" != "$dump_size" ]]; then
      die "the off-site postgres.dump is $remote_dump_size bytes, not $dump_size"
    fi
  fi
  log "Off-site copy verified"
fi

# ── Originals that live in the object store ──────────────────────────────────
# A document kept in the bucket is not in documents.tar.gz: that archive is the
# local volume, which is empty for those. Without this step their ciphertext
# would exist in exactly one place, and the nightly backup would be carrying
# the wrapped keys that open documents it does not have.
#
# `copy`, never `sync`. Object names are random per write and nothing is ever
# overwritten there, so this mirror only grows; and a backup that mirrors
# deletions is not a backup, it is a second copy of the same mistake. An object
# left here after its document was erased is unreadable noise, because the key
# that opened it went with the row.
#
# One shared directory rather than one per timestamp, because the objects are
# immutable: fourteen dated copies of the same ciphertext would be fourteen
# times the bill for no additional recovery.
if [[ -n "${BACKUP_REMOTE:-}" && -n "${MGC_BUCKET:-}" ]]; then
  if [[ "$is_scp_remote" == true ]]; then
    echo "WARNING: BACKUP_REMOTE is an scp target, so bucket originals were NOT copied" >&2
  else
    # Its own remote, never derived from BACKUP_REMOTE. The object store and
    # the backup are deliberately different providers, which is the entire
    # point of the backup, so building one name out of the other recreates the
    # coupling this step exists to break.
    #
    # That was not hypothetical: the first run of this built `b2:<object
    # bucket>` and died, because the object store is on Magalu and the backup
    # on Backblaze. It failed loudly only because the Backblaze key is scoped
    # to one bucket. On a permissive key, `rclone copy` from a bucket that is
    # empty or absent succeeds with zero files, and the backup would have
    # reported success while copying nothing at all.
    [[ -n "${MGC_REMOTE:-}" ]] || die "MGC_BUCKET is set but
MGC_REMOTE is not, so there is no rclone remote to read the bucket
through. Set it in $deploy_root/.env, for example
MGC_REMOTE=mgc:$MGC_BUCKET"
    [[ "${MGC_REMOTE#*:}" == "$MGC_BUCKET" ]] \
      || die "MGC_REMOTE points at '${MGC_REMOTE#*:}' but the
application writes to '$MGC_BUCKET'. The backup would copy a bucket
nobody is filling."

    log "Copying bucket originals from $MGC_REMOTE"
    count_of() {
      rclone size --json "$1" 2>/dev/null \
        | sed -n 's/.*"count":[[:space:]]*\([0-9]*\).*/\1/p'
    }
    in_store="$(count_of "$MGC_REMOTE")"
    [[ -n "$in_store" ]] || die "cannot read $MGC_REMOTE to count its objects"

    rclone copy "$MGC_REMOTE" "$BACKUP_REMOTE/objects" \
      || die "the bucket originals could not be copied; their only copy is in $MGC_REMOTE"

    # Counted, not trusted. A copy of nothing exits zero, and the night that
    # matters is the one where somebody asks for a document back.
    in_backup="$(count_of "$BACKUP_REMOTE/objects")"
    [[ -n "$in_backup" ]] || die "the bucket copy cannot be listed back"
    if (( in_backup < in_store )); then
      die "the bucket has $in_store objects but the backup has $in_backup"
    fi
    log "Bucket originals: $in_store in the store, $in_backup in the backup"
  fi
fi

# ── Retention ────────────────────────────────────────────────────────────────
log "Pruning local backups older than $retention_days days"
find "$backup_root" -mindepth 1 -maxdepth 1 -type d -mtime "+$retention_days" \
  -exec rm -rf {} +

# A nightly full backup that is never pruned off-site grows storage (and its
# bill) without bound. Every backup directory is named by its UTC timestamp, so
# age comes from the name — not from remote mtime, which some backends do not
# preserve on copy.
if [[ -n "${BACKUP_REMOTE:-}" ]]; then
  log "Pruning off-site backups older than $remote_retention_days days"
  cutoff_epoch=$(( $(date -u +%s) - remote_retention_days * 86400 ))
  if [[ "$is_scp_remote" == true ]]; then
    remote_host="${BACKUP_REMOTE%%:*}"
    remote_path="${BACKUP_REMOTE#*:}"
    # shellcheck disable=SC2029
    ssh "$remote_host" "find '$remote_path' -mindepth 1 -maxdepth 1 -type d -mtime +$remote_retention_days -exec rm -rf {} +" \
      || echo "WARNING: could not prune old off-site (scp) backups" >&2
  else
    while IFS= read -r entry; do
      entry_stamp="${entry%/}"
      # Backup dirs are named YYYYMMDDTHHMMSSZ; GNU date needs the ISO
      # separators reinserted before it will parse the timestamp.
      iso="${entry_stamp:0:4}-${entry_stamp:4:2}-${entry_stamp:6:2}T${entry_stamp:9:2}:${entry_stamp:11:2}:${entry_stamp:13:2}Z"
      entry_epoch=$(date -u -d "$iso" +%s 2>/dev/null) || continue
      if (( entry_epoch < cutoff_epoch )); then
        log "  removing $BACKUP_REMOTE/$entry_stamp"
        rclone purge "$BACKUP_REMOTE/$entry_stamp" \
          || echo "WARNING: could not remove $BACKUP_REMOTE/$entry_stamp" >&2
      fi
    done < <(rclone lsf --dirs-only "$BACKUP_REMOTE" 2>/dev/null)
  fi
fi

clear_alert BackupFailed "The nightly backup ran cleanly again"
log "Backup complete: $destination"
