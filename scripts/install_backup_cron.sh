#!/usr/bin/env bash
# Install the nightly backup in root's crontab, idempotently.
#
# Run as root on the VPS:
#   /opt/demo-firm-agent-v2/scripts/install_backup_cron.sh
#
# Re-running it replaces the existing entry rather than adding a second one.
set -Eeuo pipefail

deploy_root="${1:-/opt/demo-firm-agent-v2}"
schedule="${BACKUP_SCHEDULE:-10 2 * * *}"
log_file="${BACKUP_LOG:-/var/log/demo-firm-backup.log}"
marker="# demo-firm-agent backup"

[[ "$(id -u)" -eq 0 ]] || { echo "Run this as root: the backup reads Docker and writes /var/backups." >&2; exit 1; }
[[ -x "$deploy_root/scripts/backup.sh" ]] || { echo "$deploy_root/scripts/backup.sh is missing or not executable." >&2; exit 1; }

# The backup refuses to run without an off-site destination, so catching it here
# is friendlier than a cron job that fails silently every night at 02:10.
if ! grep -q '^BACKUP_REMOTE=.\+' "$deploy_root/.env" 2>/dev/null; then
  cat >&2 <<'WARNING'
WARNING: BACKUP_REMOTE is not set in .env, so every run will refuse to start.
Set it before the first scheduled run, for example:

  BACKUP_REMOTE=mgc:demo-firm-backup      # an rclone remote
  BACKUP_REMOTE=user@host:/srv/backups      # an scp target

WARNING
fi

touch "$log_file"
chmod 600 "$log_file"

entry="$schedule $deploy_root/scripts/backup.sh >> $log_file 2>&1 $marker"

# Keep every other cron line, drop any previous entry of ours, append the new one.
current="$(crontab -l 2>/dev/null || true)"
printf '%s\n' "$current" | grep -v -F "$marker" | grep -v '^$' > /tmp/crontab.new || true
printf '%s\n' "$entry" >> /tmp/crontab.new
crontab /tmp/crontab.new
rm -f /tmp/crontab.new

echo "Installed:"
crontab -l | grep -F "$marker"
echo
echo "Logs: $log_file"
echo "Test it now with: $deploy_root/scripts/backup.sh"
