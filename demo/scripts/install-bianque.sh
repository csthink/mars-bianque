#!/usr/bin/env bash
#
# Install Bianque into the demo cluster that up.sh brought up: build and load the
# backend and frontend images, generate the demo login, create the Secrets and the
# runbook ConfigMap the chart reads, install the chart (deploy/helm/bianque) and print
# the address and the login. Running it again upgrades in place; the database is kept.
#
# Required environment (set by the Makefile):
#   LLM_API_KEY_FILE  file holding the model key; read into a Secret, mounted as a file
#   LLM_BASE_URL, LLM_MODEL, LLM_EXTRA_BODY  the model, as for `make replay LLM_MODE=live`

source "$(dirname "$0")/lib.sh"

: "${LLM_API_KEY_FILE:?set LLM_API_KEY_FILE to the model key file}"
: "${LLM_BASE_URL:?set LLM_BASE_URL (see the Makefile)}"
: "${LLM_MODEL:?set LLM_MODEL (see the Makefile)}"
: "${LLM_EXTRA_BODY:?set LLM_EXTRA_BODY (see the Makefile)}"
[ -r "$LLM_API_KEY_FILE" ] || die "LLM_API_KEY_FILE is not a readable file: $LLM_API_KEY_FILE"

BACKEND_IMAGE="bianque-backend:dev"
FRONTEND_IMAGE="bianque-frontend:dev"
CHART="$REPO_ROOT/deploy/helm/bianque"

require docker kind "$KUBECTL" helm openssl
kind get clusters 2>/dev/null | grep -qx "$KIND_CLUSTER" ||
  die "no kind cluster named $KIND_CLUSTER; run make demo"

info "building and loading $BACKEND_IMAGE and $FRONTEND_IMAGE"
docker build --quiet --tag "$BACKEND_IMAGE" "$REPO_ROOT/backend" >/dev/null
docker build --quiet --tag "$FRONTEND_IMAGE" "$REPO_ROOT/frontend" >/dev/null
kind load docker-image "$BACKEND_IMAGE" "$FRONTEND_IMAGE" --name "$KIND_CLUSTER" >/dev/null

init_kind_env
"$REPO_ROOT/demo/scripts/demo-login.sh" "$KIND_LOGIN_ENV" "$KIND_PASSWORD" "$KIND_MCP_TOKEN" \
  docker run --rm -i "$BACKEND_IMAGE" bianque auth hash-password

info "creating the Secrets and the runbook ConfigMap in namespace $BIANQUE_NAMESPACE"
secret_dir="$(mktemp -d)"
trap 'rm -rf "$secret_dir"' EXIT
(
  # The login file is written for Docker Compose: values in single quotes, which the
  # shell removes when sourcing it.
  # shellcheck source=/dev/null
  source "$KIND_ENV"
  # shellcheck source=/dev/null
  source "$KIND_LOGIN_ENV"
  umask 077
  printf '%s' "$BIANQUE_DB_PASSWORD" >"$secret_dir/password"
  printf '%s' "$BIANQUE_DEMO_USERS" >"$secret_dir/BIANQUE_DEMO_USERS"
  printf '%s' "$BIANQUE_SESSION_SECRET" >"$secret_dir/BIANQUE_SESSION_SECRET"
  printf '%s' "$BIANQUE_MCP_CLIENTS" >"$secret_dir/BIANQUE_MCP_CLIENTS"
  printf '%s' "$BIANQUE_ALERTMANAGER_TOKEN" >"$secret_dir/BIANQUE_ALERTMANAGER_TOKEN"
)
kube create namespace "$BIANQUE_NAMESPACE" --dry-run=client -o yaml | kube apply -f - >/dev/null
apply_secret "$BIANQUE_NAMESPACE" bianque-db "password=$secret_dir/password"
apply_secret "$BIANQUE_NAMESPACE" bianque-auth \
  "BIANQUE_DEMO_USERS=$secret_dir/BIANQUE_DEMO_USERS" \
  "BIANQUE_SESSION_SECRET=$secret_dir/BIANQUE_SESSION_SECRET" \
  "BIANQUE_MCP_CLIENTS=$secret_dir/BIANQUE_MCP_CLIENTS" \
  "BIANQUE_ALERTMANAGER_TOKEN=$secret_dir/BIANQUE_ALERTMANAGER_TOKEN"
apply_secret "$BIANQUE_NAMESPACE" bianque-llm "api-key=$LLM_API_KEY_FILE"
kube -n "$BIANQUE_NAMESPACE" create configmap bianque-runbooks \
  --from-file="$REPO_ROOT/knowledge/runbooks" --dry-run=client -o yaml | kube apply -f - >/dev/null

# The image IDs go into the pod templates: a rebuilt image under the same tag rolls the
# pods, and an unchanged one leaves them running.
backend_id="$(docker image inspect --format '{{.Id}}' "$BACKEND_IMAGE")"
frontend_id="$(docker image inspect --format '{{.Id}}' "$FRONTEND_IMAGE")"

info "installing Bianque (chart deploy/helm/bianque)"
helm upgrade --install bianque "$CHART" \
  --kube-context "$KUBE_CONTEXT" \
  --namespace "$BIANQUE_NAMESPACE" \
  --values "$REPO_ROOT/demo/bianque/values.yaml" \
  --set-string "images.backend.id=$backend_id" \
  --set-string "images.frontend.id=$frontend_id" \
  --set-string "llm.baseUrl=$LLM_BASE_URL" \
  --set-string "llm.model=$LLM_MODEL" \
  --set-json "llm.extraBody=$LLM_EXTRA_BODY" \
  --wait --timeout 10m >/dev/null

kube get pods -n "$BIANQUE_NAMESPACE"
url="http://127.0.0.1:$DEMO_PORT"
info "Bianque (demo profile, live model $LLM_MODEL): $url"
info "demo login: demo / $(cat "$KIND_PASSWORD")"
info "MCP for Claude Code, run from the repository root:"
# The $(cat …) is printed as it is, for the user to run.
# shellcheck disable=SC2016
printf '  claude mcp add --transport http bianque %s/mcp --header "Authorization: Bearer $(cat %s)"\n' \
  "$url" "${KIND_MCP_TOKEN#"$REPO_ROOT"/}"
info "inject a fault with: make inject-s1 CASE=a"
