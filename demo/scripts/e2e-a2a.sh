#!/usr/bin/env bash
#
# Generate the two A2A clients of the end-to-end tests, new on every run: `e2e-agent-a`
# (namespace demo-s1, the namespace of the cases) and `e2e-agent-b` (namespace
# e2e-elsewhere, which holds no case). The tests check that each client sees only its
# own tasks and that B's requests for the cases are denied.
#
#   e2e-a2a.sh <env file> <token file of A> <token file of B>
#
# The env file holds BIANQUE_A2A_CLIENTS only; `make e2e` passes it to Docker Compose
# after the demo login, so that it replaces the client of the replay profile. Every file
# is written readable by the owner only.

source "$(dirname "$0")/lib.sh"

[ "$#" -eq 3 ] || die "usage: $0 <env file> <token file of A> <token file of B>"
env_file="$1"
token_a_file="$2"
token_b_file="$3"

umask 077
token_a_sha256="$(new_token "$token_a_file")"
token_b_sha256="$(new_token "$token_b_file")"
printf "BIANQUE_A2A_CLIENTS='[%s,%s]'\n" \
  "{\"name\":\"e2e-agent-a\",\"token_sha256\":\"$token_a_sha256\",\"namespaces\":[\"demo-s1\"]}" \
  "{\"name\":\"e2e-agent-b\",\"token_sha256\":\"$token_b_sha256\",\"namespaces\":[\"e2e-elsewhere\"]}" \
  >"$env_file.tmp"
mv "$token_a_file.tmp" "$token_a_file"
mv "$token_b_file.tmp" "$token_b_file"
mv "$env_file.tmp" "$env_file"
info "end-to-end A2A clients generated: tokens in $token_a_file and $token_b_file"
