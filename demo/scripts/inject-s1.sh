#!/usr/bin/env bash
#
# Inject an S1 fault: change the s1-app manifests, commit with the demo identity
# and apply. Usage: inject-s1.sh a|b|c|e
#
#   a  APP_MODE set to an invalid value; the container exits on startup
#   b  memory limit lowered below what the app allocates; the container is OOMKilled
#   c  liveness probe moved to a path the app does not serve; the kubelet restarts it
#   e  as a, then a later commit that only edits README.md
#
# The injected case and the commit it started from are kept in $DEMO_DIR/state/fault,
# outside the manifest repository, so nothing in the repository history names the
# fault. Running the same case again only re-applies; another case is refused until
# restore-s1.sh has run. Commit messages read like ordinary changes on purpose.

source "$(dirname "$0")/lib.sh"

usage() {
  die "usage: $0 a|b|c|e"
}

[ $# -eq 1 ] || usage
fault="$1"
case "$fault" in
  a | b | c | e) ;;
  *) usage ;;
esac

require_clean_manifests

if [ -f "$STATE_FILE" ]; then
  read -r active _ <"$STATE_FILE"
  if [ "$active" = "$fault" ]; then
    info "S1-$fault is already injected; re-applying the manifests"
    apply_manifests
    exit 0
  fi
  die "S1-$active is injected; run make restore-s1 first"
fi

deployment="$MANIFESTS_DIR/deployment.yaml"

set_invalid_app_mode() {
  replace_line "$deployment" "            - name: APP_MODE" \
    "              value: standard" \
    "              value: invalid"
  commit_manifests "Update app mode configuration"
}

mkdir -p "$(dirname "$STATE_FILE")"
printf '%s %s\n' "$fault" "$(demo_git rev-parse HEAD)" >"$STATE_FILE"

case "$fault" in
  a)
    set_invalid_app_mode
    ;;
  b)
    replace_line "$deployment" "            requests:" \
      "              memory: 64Mi" \
      "              memory: 32Mi"
    replace_line "$deployment" "            limits:" \
      "              memory: 128Mi" \
      "              memory: 32Mi"
    commit_manifests "Tune s1-app memory limit"
    ;;
  c)
    replace_line "$deployment" "          livenessProbe:" \
      "              path: /healthz" \
      "              path: /status"
    commit_manifests "Move liveness check to the status endpoint"
    ;;
  e)
    set_invalid_app_mode
    apply_manifests
    cat >>"$MANIFESTS_DIR/README.md" <<'EOF'

## Rollout

After applying a change, watch it with
`kubectl -n demo-s1 rollout status deployment/s1-app`.
EOF
    commit_manifests "Update deployment notes"
    ;;
esac

apply_manifests
info "S1-$fault injected; watch it with: kubectl --context $KUBE_CONTEXT -n $DEMO_NAMESPACE get pods -w"
