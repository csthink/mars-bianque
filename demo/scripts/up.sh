#!/usr/bin/env bash
#
# Bring up the demo environment: kind cluster, manifest repository, s1-app image,
# Prometheus with Alertmanager and kube-state-metrics, and the s1-app deployment.
# Bianque itself is installed by install-bianque.sh (`make demo` runs both; recording
# evidence packs needs the environment without Bianque). Every step skips or converges
# when its result already exists, so running it again changes nothing.
#
# Required environment (set by the Makefile):
#   KIND_NODE_IMAGE           kind node image, pinned with its digest
#   PROMETHEUS_CHART_VERSION  version of the prometheus-community/prometheus chart

source "$(dirname "$0")/lib.sh"

: "${KIND_NODE_IMAGE:?set KIND_NODE_IMAGE (see the Makefile)}"
: "${PROMETHEUS_CHART_VERSION:?set PROMETHEUS_CHART_VERSION (see the Makefile)}"

PROMETHEUS_CHART="oci://ghcr.io/prometheus-community/charts/prometheus"
APP_IMAGE="bianque-s1-app:dev"
KIND_CONFIG="$DEMO_DIR/kind-config.yaml"

require docker kind "$KUBECTL" helm git
docker info >/dev/null 2>&1 || die "docker is not running"

# The manifest repository must exist before the cluster: the node mounts it.
init_manifests

if kind get clusters 2>/dev/null | grep -qx "$KIND_CLUSTER"; then
  info "kind cluster exists: $KIND_CLUSTER"
else
  info "creating kind cluster: $KIND_CLUSTER"
  sed -e "s|@NODE_IMAGE@|$KIND_NODE_IMAGE|" -e "s|@MANIFESTS_DIR@|$MANIFESTS_DIR|" \
    -e "s|@DEMO_PORT@|$DEMO_PORT|" \
    "$REPO_ROOT/demo/kind/cluster.yaml.tmpl" >"$KIND_CONFIG"
  # kind switches the current kubectl context to the new cluster; switch it back so
  # creating the demo never redirects the user's own kubectl commands.
  previous_context="$("$KUBECTL" config current-context 2>/dev/null || true)"
  kind create cluster --name "$KIND_CLUSTER" --config "$KIND_CONFIG" --wait 120s
  if [ -n "$previous_context" ]; then
    "$KUBECTL" config use-context "$previous_context" >/dev/null
  fi
fi

# A cluster created from an older template has no port mapping for the Bianque pages.
mapped="$(docker port "$KIND_CLUSTER-control-plane" "$DEMO_NODE_PORT/tcp" 2>/dev/null || true)"
[ -n "$mapped" ] ||
  die "the kind cluster has no port mapping for the Bianque pages; run make demo-down first"

info "building and loading $APP_IMAGE"
docker build --quiet --tag "$APP_IMAGE" "$REPO_ROOT/demo/s1-app" >/dev/null
kind load docker-image "$APP_IMAGE" --name "$KIND_CLUSTER" >/dev/null

# Alertmanager reads the webhook token from this Secret; it must exist before the chart.
init_kind_env
token_dir="$(mktemp -d)"
trap 'rm -rf "$token_dir"' EXIT
(
  # shellcheck source=/dev/null
  source "$KIND_ENV"
  umask 077
  printf '%s' "$BIANQUE_ALERTMANAGER_TOKEN" >"$token_dir/token"
)
kube create namespace "$MONITORING_NAMESPACE" --dry-run=client -o yaml | kube apply -f - >/dev/null
apply_secret "$MONITORING_NAMESPACE" bianque-alertmanager-token "token=$token_dir/token"

info "installing Prometheus and Alertmanager (chart $PROMETHEUS_CHART_VERSION)"
helm upgrade --install prometheus "$PROMETHEUS_CHART" \
  --version "$PROMETHEUS_CHART_VERSION" \
  --kube-context "$KUBE_CONTEXT" \
  --namespace "$MONITORING_NAMESPACE" --create-namespace \
  --values "$REPO_ROOT/demo/monitoring/values.yaml" \
  --values "$REPO_ROOT/demo/monitoring/alert-rules.yaml" \
  --wait --timeout 5m >/dev/null

info "deploying s1-app from the manifest repository"
deploy_s1_app

kube get pods -n "$MONITORING_NAMESPACE"
kube get pods -n "$DEMO_NAMESPACE"
info "demo environment is up"
