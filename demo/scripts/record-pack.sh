#!/usr/bin/env bash
#
# Record the evidence pack of one S1 case from the demo cluster.
# Usage: record-pack.sh a|b|c|e
#
# 1. Reset demo-s1 to a clean baseline, so the pack holds only the baseline and this
#    case's commits and revisions.
# 2. Deploy the capture receiver (demo/monitoring/capture-receiver.yaml) on the
#    webhook address and inject the fault.
# 3. Wait for the first firing notification about demo-s1 sent after the injection
#    and keep its body as the case's alert.
# 4. Record the pack into eval/cases/s1-<case>/pack.
# 5. Run the kind integration tests against the live cluster and the new pack.
# 6. Restore the fault and remove the receiver.
#
# On failure the fault stays injected for inspection; the receiver is always removed.

source "$(dirname "$0")/lib.sh"

case="${1:-}"
case "$case" in
  a | b | c | e) ;;
  *) die "usage: record-pack.sh a|b|c|e" ;;
esac

require "$KUBECTL" kind jq git uv
RECEIVER="$REPO_ROOT/demo/monitoring/capture-receiver.yaml"
WORK_DIR="$DEMO_DIR/record/s1-$case"
PACK_DIR="$REPO_ROOT/eval/cases/s1-$case/pack"
ALERT_TIMEOUT=300

# Wait until the receiver's namespace is gone: the next recording creates it again,
# and a namespace that is still terminating refuses new objects.
cleanup() {
  kube delete -f "$RECEIVER" --ignore-not-found --wait=true --timeout="$ROLLOUT_TIMEOUT" \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT

mkdir -p "$WORK_DIR"
reset_demo

info "deploying the capture receiver"
cleanup
kube apply -f "$RECEIVER" >/dev/null
kube -n bianque rollout status deployment/bianque-backend --timeout="$ROLLOUT_TIMEOUT"

since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
"$REPO_ROOT/demo/scripts/inject-s1.sh" "$case"

info "waiting for the firing notification (up to ${ALERT_TIMEOUT}s)"
alert=""
waited=0
while [ -z "$alert" ]; do
  [ "$waited" -lt "$ALERT_TIMEOUT" ] || die "no firing notification about $DEMO_NAMESPACE after ${ALERT_TIMEOUT}s"
  sleep 5
  waited=$((waited + 5))
  alert="$(kube -n bianque logs deployment/bianque-backend 2>/dev/null |
    sed -n 's/^WEBHOOK //p' |
    jq -c --arg ns "$DEMO_NAMESPACE" --arg since "$since" '
      select(.status == "firing" and any(.alerts[];
        .status == "firing" and .labels.namespace == $ns and .startsAt >= $since))' |
    head -n 1)"
done
printf '%s\n' "$alert" >"$WORK_DIR/alert.json"
info "notification received after ${waited}s"

kind_version="$(kind version | awk '{print $2}')"
kube_version="$(kube version -o json | jq -r .serverVersion.gitVersion)"
(
  cd "$REPO_ROOT/backend"
  uv run --locked bianque pack record \
    --namespace "$DEMO_NAMESPACE" \
    --alert "$WORK_DIR/alert.json" \
    --repo "$MANIFESTS_DIR" \
    --case "s1-$case" \
    --out "$PACK_DIR" \
    --context "$KUBE_CONTEXT" \
    --meta "kind=$kind_version" \
    --meta "kubernetes=$kube_version" \
    --meta "prometheus_chart=${PROMETHEUS_CHART_VERSION:-unknown}"
)

info "running the kind integration tests"
(
  cd "$REPO_ROOT/backend"
  BIANQUE_TEST_KIND_CASE="$case" \
    BIANQUE_TEST_KIND_CONTEXT="$KUBE_CONTEXT" \
    BIANQUE_TEST_KIND_REPO="$MANIFESTS_DIR" \
    uv run --locked pytest tests/tools/test_tool_kind.py -v 2>&1 | tee "$WORK_DIR/kind-tests.log"
  exit "${PIPESTATUS[0]}"
)

"$REPO_ROOT/demo/scripts/restore-s1.sh"
info "recorded $PACK_DIR"
