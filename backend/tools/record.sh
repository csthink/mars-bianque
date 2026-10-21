#!/usr/bin/env bash
# Record the model responses of every case with a real model.
#
# 1. In a throwaway database, evaluate every case in live mode and record every model
#    response into a new directory, one subdirectory per case, with the manifest.
# 2. Keep the live run's report in a new eval/reports/<date>-<model>[-<n>]/ at once,
#    whatever the run's result: every live run's report stays, and none is overwritten.
# 3. In a second throwaway database, evaluate again in recorded mode from that
#    directory, and check that the replay reproduced the live run.
# 4. Only then replace eval/recordings with the new recordings.
#
# Usage: backend/tools/record.sh <work directory>
# Needs LLM_API_KEY_FILE (a file holding the model key), LLM_MODEL, LLM_BASE_URL and
# LLM_EXTRA_BODY. Embeddings come from the fixture, as in every replay, so that the
# retrieval results and with them the prompts are the same.
set -euo pipefail

: "${LLM_API_KEY_FILE:?set LLM_API_KEY_FILE to the model key file}"
: "${LLM_MODEL:?set LLM_MODEL}"
: "${LLM_BASE_URL:?set LLM_BASE_URL}"
test -r "$LLM_API_KEY_FILE" || { echo "cannot read $LLM_API_KEY_FILE" >&2; exit 2; }

root=$(cd "$(dirname "$0")/../.." && pwd)
mkdir -p "$1"
work=$(cd "$1" && pwd)
# The key is read from the file by the settings: a file: reference, never the key itself.
key_ref="file:$(cd "$(dirname "$LLM_API_KEY_FILE")" && pwd)/$(basename "$LLM_API_KEY_FILE")"
# Every database container of this run carries the label, so the cleanup finds them
# although each is started in a command substitution's subshell.
label="bianque-record=$$"
cleanup() {
  docker ps --all --quiet --filter "label=$label" | xargs docker rm --force >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Start a throwaway PostgreSQL, migrate it, and print its URL.
database() {
  local name password port
  name="bianque-record-$(openssl rand -hex 6)"
  password=$(openssl rand -hex 16)
  docker run --detach --rm --name "$name" --label "$label" --env "POSTGRES_PASSWORD=$password" \
    --publish 127.0.0.1::5432 pgvector/pgvector:pg16 >/dev/null
  until docker exec "$name" pg_isready -U postgres >/dev/null 2>&1; do sleep 0.5; done
  port=$(docker port "$name" 5432/tcp | head -n 1 | sed 's/.*://')
  local url="postgresql+psycopg://postgres:$password@127.0.0.1:$port/postgres"
  # pg_isready answers before the server accepts TCP connections from the host.
  for _ in $(seq 1 60); do
    BIANQUE_DATABASE_URL="$url" uv run --locked alembic upgrade head >/dev/null 2>&1 && break
    sleep 0.5
  done
  BIANQUE_DATABASE_URL="$url" uv run --locked alembic upgrade head >/dev/null
  echo "$url"
}

cd "$root/backend"
export BIANQUE_TOOL_BACKEND=pack BIANQUE_EMBEDDING_MODE=fixture \
  BIANQUE_EMBEDDING_FIXTURE="$root/eval/embeddings/bge-small-en-v1.5.json" \
  BIANQUE_KNOWLEDGE_DIR="$root/knowledge/runbooks" BIANQUE_SKILLS_DIR="$root/.agents/skills"
evaluate=(uv run --locked bianque eval run --cases "$root/eval/cases"
  --queries "$root/eval/retrieval/queries.yaml")

echo "recording every case with $LLM_MODEL into $work/recordings"
status=0
BIANQUE_DATABASE_URL=$(database) BIANQUE_LLM_API_KEY="$key_ref" \
  BIANQUE_LLM_BASE_URL="$LLM_BASE_URL" BIANQUE_LLM_MODEL="$LLM_MODEL" \
  BIANQUE_LLM_EXTRA_BODY="${LLM_EXTRA_BODY:-}" \
  "${evaluate[@]}" --llm-mode live --record "$work/recordings" --out "$work/live" || status=$?

# The report of every live run is kept, in a directory no earlier run used.
base="$root/eval/reports/$(date -u +%Y-%m-%d)-$LLM_MODEL"
reports="$base"
n=2
while [ -e "$reports" ]; do
  reports="$base-$n"
  n=$((n + 1))
done
if [ -f "$work/live/report.json" ]; then
  mkdir -p "$reports"
  cp "$work/live/report.json" "$work/live/report.md" "$reports/"
  echo "live report: ${reports#"$root"/}"
fi
if [ "$status" -ne 0 ]; then
  echo "the live run failed (exit $status); eval/recordings is unchanged" >&2
  exit "$status"
fi

echo "replaying the recordings"
BIANQUE_DATABASE_URL=$(database) BIANQUE_LLM_RECORDINGS_DIR="$work/recordings" \
  "${evaluate[@]}" --llm-mode recorded --out "$work/replay"
uv run --locked bianque eval compare "$work/live/report.json" "$work/replay/report.json"

rm -rf "$root/eval/recordings"
mv "$work/recordings" "$root/eval/recordings"
echo "recordings: eval/recordings; live report: ${reports#"$root"/}"
