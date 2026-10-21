#!/usr/bin/env bash
#
# Delete the demo kind cluster and the secrets of the Bianque installation in it
# ($KIND_SECRETS_DIR), whose database goes with the cluster. The rest of the local state
# in $DEMO_DIR (manifest repository, generated kind config, injected-fault state, the
# replay profile's files) is kept; delete it as well to start from scratch.

source "$(dirname "$0")/lib.sh"

require kind
if kind get clusters 2>/dev/null | grep -qx "$KIND_CLUSTER"; then
  kind delete cluster --name "$KIND_CLUSTER"
else
  info "no kind cluster named $KIND_CLUSTER"
fi
rm -rf "$KIND_SECRETS_DIR"
