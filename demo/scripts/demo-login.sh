#!/usr/bin/env bash
#
# Generate the demo login: a random password for the account `demo` and a random
# session secret, new on every run, so that no default password is ever committed and
# the sessions of an earlier run stop working.
#
#   demo-login.sh <env file> <password file> <hash command...>
#
# The password goes to the hash command on standard input (never as an argument) and
# the command prints its bcrypt hash; `bianque auth hash-password` is that command. The
# env file is read by Docker Compose with --env-file: the users value is single-quoted,
# so the `$` signs of the hash stay as they are. Both files are written readable by the
# owner only.

source "$(dirname "$0")/lib.sh"

[ "$#" -ge 3 ] || die "usage: $0 <env file> <password file> <hash command...>"
env_file="$1"
password_file="$2"
shift 2

umask 077
password="$(openssl rand -hex 16)"
secret="$(openssl rand -hex 32)"
hash="$(printf '%s\n' "$password" | "$@")" || die "the hash command failed"
[[ "$hash" =~ ^\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}$ ]] ||
  die "the hash command did not print a bcrypt hash"

# Write next to the targets and rename, so that a failed run leaves the old files whole.
printf '%s\n' "$password" >"$password_file.tmp"
printf "BIANQUE_DEMO_USERS='{\"demo\":\"%s\"}'\nBIANQUE_SESSION_SECRET=%s\n" \
  "$hash" "$secret" >"$env_file.tmp"
mv "$password_file.tmp" "$password_file"
mv "$env_file.tmp" "$env_file"
info "demo login generated: user demo, password in $password_file"
