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

read -r active base <"$STATE_FILE"
# Compare trees rather than counting commits: when an earlier run reverted but then
# failed before clearing the state, reverting base..HEAD again would re-inject.
if ! demo_git diff --quiet "$base" HEAD; then
  # git revert walks the range newest first, so later commits are undone first.
  demo_git revert --no-edit "$base..HEAD" >/dev/null
  info "reverted the S1-$active commits; HEAD is $(demo_git rev-parse --short HEAD)"
fi
apply_manifests
wait_rollout
rm -f "$STATE_FILE"
info "S1-$active restored"
