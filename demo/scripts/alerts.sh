#!/usr/bin/env bash
#
# Print the alerts Alertmanager currently holds, read through the API server proxy
# (no port-forward). Usage: alerts.sh [--json]
#
# One line per alert: start time, state, alert name, namespace/pod/container and
# fingerprint. Resolved alerts are not listed. --json prints the raw API response.

source "$(dirname "$0")/lib.sh"

ALERTS_PATH="/api/v1/namespaces/monitoring/services/http:prometheus-alertmanager:9093/proxy/api/v2/alerts"

require "$KUBECTL" jq
raw="$(kube get --raw "$ALERTS_PATH")"
if [ "${1:-}" = "--json" ]; then
  printf '%s\n' "$raw"
  exit 0
fi
printf '%s\n' "$raw" | jq -r '
  ["STARTS_AT", "STATE", "ALERT", "TARGET", "FINGERPRINT"],
  (sort_by(.startsAt)[] | [
    .startsAt,
    .status.state,
    .labels.alertname,
    "\(.labels.namespace // "-")/\(.labels.pod // "-")/\(.labels.container // "-")",
    .fingerprint
  ])
  | @tsv' | column -t -s "$(printf '\t')"
