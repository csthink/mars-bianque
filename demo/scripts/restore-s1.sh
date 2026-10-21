#!/usr/bin/env bash
#
# Restore after inject-s1.sh: revert every commit made since the fault was
# injected (one revert commit each, with the demo identity), apply and wait for the
# rollout. Without an injected fault it only re-applies, so running it twice is
# harmless.

source "$(dirname "$0")/lib.sh"

require_clean_manifests

if [ ! -f "$STATE_FILE" ]; then
  info "no fault is injected; re-applying the manifests"
  apply_manifests
  wait_rollout
  exit 0
fi

active="$(injected_fault)"
revert_fault
apply_manifests
wait_rollout
rm -f "$STATE_FILE"
info "S1-$active restored"
