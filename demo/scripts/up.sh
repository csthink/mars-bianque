#!/usr/bin/env bash
#
# Bring up the demo environment: kind cluster, manifest repository, s1-app image,
# Prometheus with Alertmanager and kube-state-metrics, and the s1-app deployment.
# Every step skips or converges when its result already exists, so running it
# again changes nothing.
#
# Required environment (set by the Makefile):
#   KIND_NODE_IMAGE           kind node image, pinned with its digest
#   PROMETHEUS_CHART_VERSION  version of the prometheus-community/prometheus chart

source "$(dirname "$0")/lib.sh"

: "${KIND_NODE_IMAGE:?set KIND_NODE_IMAGE (see the Makefile)}"
: "${PROMETHEUS_CHART_VERSION:?set PROMETHEUS_CHART_VERSION (see the Makefile)}"

PROMETHEUS_CHART="oci://ghcr.io/prometheus-community/charts/prometheus"
MONITORING_NAMESPACE="monitoring"
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
    "$REPO_ROOT/demo/kind/cluster.yaml.tmpl" >"$KIND_CONFIG"
  # kind switches the current kubectl context to the new cluster; switch it back so
  # creating the demo never redirects the user's own kubectl commands.
  previous_context="$("$KUBECTL" config current-context 2>/dev/null || true)"
  kind create cluster --name "$KIND_CLUSTER" --config "$KIND_CONFIG" --wait 120s
  if [ -n "$previous_context" ]; then
    "$KUBECTL" config use-context "$previous_context" >/dev/null
  fi
fi

info "building and loading $APP_IMAGE"
docker build --quiet --tag "$APP_IMAGE" "$REPO_ROOT/demo/s1-app" >/dev/null
kind load docker-image "$APP_IMAGE" --name "$KIND_CLUSTER" >/dev/null

info "installing Prometheus and Alertmanager (chart $PROMETHEUS_CHART_VERSION)"
helm upgrade --install prometheus "$PROMETHEUS_CHART" \
  --version "$PROMETHEUS_CHART_VERSION" \
  --kube-context "$KUBE_CONTEXT" \
  --namespace "$MONITORING_NAMESPACE" --create-namespace \
  --values "$REPO_ROOT/demo/monitoring/values.yaml" \
  --values "$REPO_ROOT/demo/monitoring/alert-rules.yaml" \
  --wait --timeout 5m >/dev/null

info "deploying s1-app from the manifest repository"
apply_manifests
wait_rollout

kube get pods -n "$MONITORING_NAMESPACE"
kube get pods -n "$DEMO_NAMESPACE"
info "demo environment is up; inject a fault with: make inject-s1 CASE=a"
