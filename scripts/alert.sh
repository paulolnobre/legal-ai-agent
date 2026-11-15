# shellcheck shell=bash
# Sourced, not run: lets a host script raise or clear an alert through the
# Alertmanager that already delivers Prometheus's alerts to the firm's chat.
#
# Host jobs (the nightly backup, the restore drill) have no metrics endpoint
# for Prometheus to scrape, so until now a failed backup was a line in a log
# file nobody reads. Alertmanager's API accepts an alert from anyone who can
# reach it, and it is published on 127.0.0.1 of this host only, so the job
# speaks to it directly and the message arrives in the same place and the same
# format as every other alert.
#
# Best effort by design: an unreachable Alertmanager is logged and never turns
# into the job's own exit status, because the job's result is the thing being
# reported and must not be replaced by the failure to report it.
#
#   raise_alert <name> <hours> <summary> <description>
#   clear_alert <name> <summary>

alertmanager_url="${ALERTMANAGER_URL:-http://127.0.0.1:9093}"

# One alert, active until $2. Alertmanager resolves it by itself at that time,
# so a job that dies before it can clear the alert does not leave it firing
# forever, and one that runs again before then either clears it or renews it.
_post_alert() {
  local name="$1" ends_at="$2" summary="$3" description="$4"
  local payload
  payload="$(jq -n \
    --arg name "$name" --arg ends "$ends_at" \
    --arg summary "$summary" --arg description "$description" \
    '[{labels: {alertname: $name, severity: "critical", job: "host"},
       annotations: {summary: $summary, description: $description},
       endsAt: $ends}]')" || { echo "WARNING: could not build the alert $name" >&2; return 0; }
  curl --fail --silent --show-error --max-time 10 \
    -H 'Content-Type: application/json' \
    -d "$payload" "$alertmanager_url/api/v2/alerts" >/dev/null \
    || echo "WARNING: Alertmanager at $alertmanager_url did not take the alert $name" >&2
  return 0
}

raise_alert() {
  local ends_at
  ends_at="$(date -u -d "+$2 hours" +%Y-%m-%dT%H:%M:%SZ)"
  _post_alert "$1" "$ends_at" "$3" "$4"
}

# Posting the same labels with an end time in the past resolves it, which
# sends the green message when it was firing and nothing when it was not. The
# summary is what that green message says, so it names what recovered.
clear_alert() {
  _post_alert "$1" "$(date -u -d '-1 minute' +%Y-%m-%dT%H:%M:%SZ)" "$2" ""
}
