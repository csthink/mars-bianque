#!/usr/bin/env bash
#
# Delete the demo kind cluster. The local state in $DEMO_DIR (manifest repository,
# generated kind config, injected-fault state) is kept; delete it as well to start
# from scratch.

source "$(dirname "$0")/lib.sh"

require kind
if kind get clusters 2>/dev/null | grep -qx "$KIND_CLUSTER"; then
  kind delete cluster --name "$KIND_CLUSTER"
else
  info "no kind cluster named $KIND_CLUSTER"
fi
