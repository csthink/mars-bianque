#!/usr/bin/env bash
#
# Generate the demo login: a random password for the account `demo`, a random session
# secret, a random MCP token for the client `claude-code` (every tool, namespace
# demo-s1) and a random A2A token for the client `demo-agent` (namespace demo-s1), new
# on every run, so that no default password or token is ever committed and the sessions
# and tokens of an earlier run stop working.
#
#   demo-login.sh <env file> <password file> <MCP token file> <A2A token file> \
#     <hash command...>
#
# The password goes to the hash command on standard input (never as an argument) and
# the command prints its bcrypt hash; `bianque auth hash-password` is that command. The
# env file is read by Docker Compose with --env-file: the users value is single-quoted,
# so the `$` signs of the hash stay as they are. The MCP and A2A clients are configured
# with the SHA-256 of their tokens only. Every file is written readable by the owner
# only.

source "$(dirname "$0")/lib.sh"

[ "$#" -ge 5 ] ||
  die "usage: $0 <env file> <password file> <MCP token file> <A2A token file> <hash command...>"
env_file="$1"
password_file="$2"
mcp_token_file="$3"
a2a_token_file="$4"
shift 4

umask 077
password="$(openssl rand -hex 16)"
secret="$(openssl rand -hex 32)"
hash="$(printf '%s\n' "$password" | "$@")" || die "the hash command failed"
[[ "$hash" =~ ^\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}$ ]] ||
  die "the hash command did not print a bcrypt hash"
mcp_token_sha256="$(new_token "$mcp_token_file")"
a2a_token_sha256="$(new_token "$a2a_token_file")"

# Write next to the targets and rename, so that a failed run leaves the old files whole.
printf '%s\n' "$password" >"$password_file.tmp"
{
  printf "BIANQUE_DEMO_USERS='{\"demo\":\"%s\"}'\nBIANQUE_SESSION_SECRET=%s\n" "$hash" "$secret"
  printf "BIANQUE_MCP_CLIENTS='[{\"name\":\"claude-code\",\"token_sha256\":\"%s\",%s}]'\n" \
    "$mcp_token_sha256" '"tools":["*"],"namespaces":["demo-s1"]'
  printf "BIANQUE_A2A_CLIENTS='[{\"name\":\"demo-agent\",\"token_sha256\":\"%s\",%s}]'\n" \
    "$a2a_token_sha256" '"namespaces":["demo-s1"]'
} >"$env_file.tmp"
mv "$password_file.tmp" "$password_file"
mv "$mcp_token_file.tmp" "$mcp_token_file"
mv "$a2a_token_file.tmp" "$a2a_token_file"
mv "$env_file.tmp" "$env_file"
info "demo login generated: user demo, password in $password_file," \
  "MCP token in $mcp_token_file, A2A token in $a2a_token_file"
