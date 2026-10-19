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

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEMO_DIR="${DEMO_DIR:-$REPO_ROOT/.demo}"
MANIFESTS_DIR="$DEMO_DIR/manifests"
STATE_FILE="$DEMO_DIR/state/fault"
KUBECTL="${KUBECTL:-kubectl}"
KIND_CLUSTER="${KIND_CLUSTER:-bianque-demo}"
KUBE_CONTEXT="kind-$KIND_CLUSTER"

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
