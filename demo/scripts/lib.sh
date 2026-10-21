# shellcheck shell=bash
# The settings below are read by the scripts that source this file.
# shellcheck disable=SC2034
#
# Shared settings and helpers for the demo scripts. Sourced, never executed.
#
# Compatible with the bash 3.2 shipped with macOS.
#
# Overridable from the environment:
#   DEMO_DIR      local demo state (default: <repo>/.demo); holds the generated kind
#                 config, the manifest repository and the injected-fault state
#   KUBECTL       kubectl binary (default: kubectl); the tests set it to `true` to
#                 exercise the manifest repository logic without a cluster
#   KIND_CLUSTER  kind cluster name (default: bianque-demo)
#   BIANQUE_DEMO_PORT  port of the Bianque pages on 127.0.0.1 (default: 8180); takes
#                 effect when the kind cluster is created

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEMO_DIR="${DEMO_DIR:-$REPO_ROOT/.demo}"
MANIFESTS_DIR="$DEMO_DIR/manifests"
STATE_FILE="$DEMO_DIR/state/fault"
KUBECTL="${KUBECTL:-kubectl}"
KIND_CLUSTER="${KIND_CLUSTER:-bianque-demo}"
KUBE_CONTEXT="kind-$KIND_CLUSTER"

DEMO_PORT="${BIANQUE_DEMO_PORT:-8180}"
# The node port of the Bianque frontend Service (demo/bianque/values.yaml), mapped to
# DEMO_PORT by the kind config.
DEMO_NODE_PORT=30080

# Secrets of the Bianque installation in the kind cluster, apart from those of the
# replay profile: the database password and the Alertmanager webhook token, generated
# once (the database volume keeps its password), and the demo login, new on every run.
KIND_SECRETS_DIR="$DEMO_DIR/kind"
KIND_ENV="$KIND_SECRETS_DIR/demo.env"
KIND_LOGIN_ENV="$KIND_SECRETS_DIR/login.env"
KIND_PASSWORD="$KIND_SECRETS_DIR/demo-password"
KIND_MCP_TOKEN="$KIND_SECRETS_DIR/mcp-token"

BIANQUE_NAMESPACE="bianque"
MONITORING_NAMESPACE="monitoring"
DEMO_NAMESPACE="demo-s1"
DEMO_DEPLOYMENT="s1-app"
ROLLOUT_TIMEOUT="180s"

# Every commit in the manifest repository carries this identity and nothing else:
# the repository history is recorded into evidence packs.
DEMO_GIT_NAME="Bianque Demo"
DEMO_GIT_EMAIL="demo@example.com"

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

info() {
  printf '==> %s\n' "$*"
}

warn() {
  printf 'warning: %s\n' "$*" >&2
}

require() {
  local cmd
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null 2>&1 || die "$cmd is required but not installed"
  done
}

# kubectl pinned to the demo cluster, whatever the current context is.
kube() {
  "$KUBECTL" --context "$KUBE_CONTEXT" "$@"
}

# git in the manifest repository with the demo identity. The user's global and
# system git configuration are ignored, so commit signing, hooks and templates
# configured on this machine never apply to the demo repository.
demo_git() {
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
    GIT_AUTHOR_NAME="$DEMO_GIT_NAME" GIT_AUTHOR_EMAIL="$DEMO_GIT_EMAIL" \
    GIT_COMMITTER_NAME="$DEMO_GIT_NAME" GIT_COMMITTER_EMAIL="$DEMO_GIT_EMAIL" \
    git -C "$MANIFESTS_DIR" "$@"
}

manifests_exist() {
  [ -d "$MANIFESTS_DIR/.git" ]
}

# Create the manifest repository from demo/s1-app/manifests; a no-op when it exists.
init_manifests() {
  if manifests_exist; then
    info "manifest repository exists: $MANIFESTS_DIR"
    return
  fi
  info "creating manifest repository: $MANIFESTS_DIR"
  mkdir -p "$MANIFESTS_DIR"
  cp "$REPO_ROOT"/demo/s1-app/manifests/* "$MANIFESTS_DIR"/
  demo_git init -q -b main
  demo_git add -A
  demo_git commit -q -m "Add s1-app manifests"
  demo_git tag baseline
}

# Fail unless the manifest repository exists and has no uncommitted changes.
require_clean_manifests() {
  manifests_exist || die "no manifest repository at $MANIFESTS_DIR; run make demo first"
  [ -z "$(demo_git status --porcelain)" ] ||
    die "the manifest repository has uncommitted changes: $MANIFESTS_DIR"
}

# replace_line <file> <anchor> <old> <new>
# Replace the first line equal to <old> that follows the line equal to <anchor>
# (anywhere in the file when <anchor> is empty). Fails when nothing is replaced, so
# a template change cannot silently turn an injection into a no-op.
replace_line() {
  local file="$1" anchor="$2" old="$3" new="$4" tmp
  tmp="$(mktemp "$file.XXXXXX")"
  if ! awk -v anchor="$anchor" -v old="$old" -v new="$new" '
    BEGIN { armed = (anchor == ""); done = 0 }
    !done && armed && $0 == old { print new; done = 1; next }
    $0 == anchor { armed = 1 }
    { print }
    END { exit done ? 0 : 1 }
  ' "$file" >"$tmp"; then
    rm -f "$tmp"
    die "expected line not found in $file: $old"
  fi
  mv "$tmp" "$file"
}

# Commit every change in the manifest repository with the given message.
commit_manifests() {
  demo_git add -A
  demo_git commit -q -m "$1"
  info "committed $(demo_git rev-parse --short HEAD): $1"
}

apply_manifests() {
  kube apply -f "$MANIFESTS_DIR"
}

wait_rollout() {
  kube -n "$DEMO_NAMESPACE" rollout status "deployment/$DEMO_DEPLOYMENT" --timeout="$ROLLOUT_TIMEOUT"
}

# Print the injected S1 case; nothing when no fault is injected.
injected_fault() {
  [ -f "$STATE_FILE" ] || return 0
  local active base
  read -r active base <"$STATE_FILE"
  printf '%s\n' "$active"
}

# Revert every commit made in the manifest repository since the fault was injected, one
# revert commit each with the demo identity. The fault state is left to the caller, and
# so is the cluster.
revert_fault() {
  local active base
  read -r active base <"$STATE_FILE"
  # Compare trees rather than counting commits: when an earlier run reverted but then
  # failed before clearing the state, reverting base..HEAD again would re-inject.
  if ! demo_git diff --quiet "$base" HEAD; then
    # git revert walks the range newest first, so later commits are undone first.
    demo_git revert --no-edit "$base..HEAD" >/dev/null
    info "reverted the S1-$active commits; HEAD is $(demo_git rev-parse --short HEAD)"
  fi
}

# Apply the manifests and wait for the s1-app rollout. With a fault injected the
# rollout is expected to fail, so say how to restore instead of waiting for a timeout.
deploy_s1_app() {
  local active
  active="$(injected_fault)"
  apply_manifests
  if [ -n "$active" ]; then
    warn "S1-$active is injected, so s1-app is expected to fail; restore it with: make restore-s1"
    return
  fi
  wait_rollout
}

# Put demo-s1 back to a clean baseline: delete the namespace, recreate the manifest
# repository with only the baseline commit, apply and wait. The repository is emptied
# and re-initialized in place: the kind node mounts this directory, and replacing it
# would leave the mount pointing at a deleted directory.
reset_demo() {
  info "deleting namespace $DEMO_NAMESPACE"
  kube delete namespace "$DEMO_NAMESPACE" --ignore-not-found --wait=true --timeout="$ROLLOUT_TIMEOUT"
  if [ -d "$MANIFESTS_DIR" ]; then
    find "$MANIFESTS_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  fi
  rm -f "$STATE_FILE"
  init_manifests
  apply_manifests
  wait_rollout
}

# Generate $KIND_ENV once: the database password and the Alertmanager webhook token.
init_kind_env() {
  [ -f "$KIND_ENV" ] && return
  mkdir -p "$KIND_SECRETS_DIR"
  (
    umask 077
    printf 'BIANQUE_DB_PASSWORD=%s\nBIANQUE_ALERTMANAGER_TOKEN=%s\n' \
      "$(openssl rand -hex 16)" "$(openssl rand -hex 32)" >"$KIND_ENV.tmp"
  )
  mv "$KIND_ENV.tmp" "$KIND_ENV"
}

# apply_secret <namespace> <name> <key=file>...
# Create or update a generic Secret from files, without the values ever being command
# line arguments.
apply_secret() {
  local namespace="$1" name="$2" entry
  shift 2
  local args=()
  for entry in "$@"; do
    args+=(--from-file="$entry")
  done
  kube -n "$namespace" create secret generic "$name" "${args[@]}" --dry-run=client -o yaml |
    kube apply -f - >/dev/null
}

# Whether the Bianque chart is installed in the demo cluster.
bianque_installed() {
  [ "$(kube -n "$BIANQUE_NAMESPACE" get deployment bianque-backend --ignore-not-found \
    -o jsonpath='{.metadata.labels.app\.kubernetes\.io/managed-by}')" = "Helm" ]
}
