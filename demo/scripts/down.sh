#!/usr/bin/env bash
#
# Delete the demo kind cluster and the secrets of the Bianque installation in it
# ($KIND_SECRETS_DIR), whose database goes with the cluster, and revert an injected S1
# fault in the manifest repository, as restore-s1.sh does but without a cluster to apply
# to: the repository outlives the cluster, and the next make demo would deploy the fault
# again. The rest of the local state in $DEMO_DIR (manifest repository with its history,
# generated kind config, the replay profile's files) is kept; delete it as well to start
# from scratch.

source "$(dirname "$0")/lib.sh"

require kind
if kind get clusters 2>/dev/null | grep -qx "$KIND_CLUSTER"; then
  kind delete cluster --name "$KIND_CLUSTER"
else
  info "no kind cluster named $KIND_CLUSTER"
fi
rm -rf "$KIND_SECRETS_DIR"

active="$(injected_fault)"
if [ -n "$active" ]; then
  if ! manifests_exist; then
    # init_manifests recreates the repository at the baseline.
    rm -f "$STATE_FILE"
  elif [ -n "$(demo_git status --porcelain)" ]; then
    warn "S1-$active stays injected: the manifest repository has uncommitted changes: $MANIFESTS_DIR"
  else
    revert_fault
    rm -f "$STATE_FILE"
    info "S1-$active reverted in the manifest repository"
  fi
fi
