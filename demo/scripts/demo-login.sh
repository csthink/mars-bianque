#!/usr/bin/env bash
#
# Generate the demo login: a random password for the account `demo`, a random session
# secret and a random MCP token for the client `claude-code` (every tool, namespace
# demo-s1), new on every run, so that no default password or token is ever committed
# and the sessions and tokens of an earlier run stop working.
#
#   demo-login.sh <env file> <password file> <token file> <hash command...>
#
# The password goes to the hash command on standard input (never as an argument) and
# the command prints its bcrypt hash; `bianque auth hash-password` is that command. The
# env file is read by Docker Compose with --env-file: the users value is single-quoted,
# so the `$` signs of the hash stay as they are. The MCP client is configured with the
# SHA-256 of its token only. All three files are written readable by the owner only.

source "$(dirname "$0")/lib.sh"

[ "$#" -ge 4 ] || die "usage: $0 <env file> <password file> <token file> <hash command...>"
env_file="$1"
password_file="$2"
token_file="$3"
shift 3

umask 077
password="$(openssl rand -hex 16)"
secret="$(openssl rand -hex 32)"
token="$(openssl rand -hex 32)"
token_sha256="$(printf '%s' "$token" | openssl dgst -sha256 -r | cut -d' ' -f1)"
[[ "$token_sha256" =~ ^[0-9a-f]{64}$ ]] || die "the token hash could not be computed"
hash="$(printf '%s\n' "$password" | "$@")" || die "the hash command failed"
[[ "$hash" =~ ^\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}$ ]] ||
  die "the hash command did not print a bcrypt hash"

# Write next to the targets and rename, so that a failed run leaves the old files whole.
printf '%s\n' "$password" >"$password_file.tmp"
printf '%s\n' "$token" >"$token_file.tmp"
{
  printf "BIANQUE_DEMO_USERS='{\"demo\":\"%s\"}'\nBIANQUE_SESSION_SECRET=%s\n" "$hash" "$secret"
  printf "BIANQUE_MCP_CLIENTS='[{\"name\":\"claude-code\",\"token_sha256\":\"%s\",%s}]'\n" \
    "$token_sha256" '"tools":["*"],"namespaces":["demo-s1"]'
} >"$env_file.tmp"
mv "$password_file.tmp" "$password_file"
mv "$token_file.tmp" "$token_file"
mv "$env_file.tmp" "$env_file"
info "demo login generated: user demo, password in $password_file, MCP token in $token_file"
