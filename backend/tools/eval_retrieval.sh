#!/usr/bin/env bash
# Retrieval evaluation in a throwaway database: start a temporary pgvector container,
# migrate, import the runbooks, evaluate the annotated queries, remove the container.
# Usage: backend/tools/eval_retrieval.sh <output directory>
# BIANQUE_EMBEDDING_MODE picks the embeddings (fixture by default).
set -euo pipefail

mkdir -p "$1"
out=$(cd "$1" && pwd)
root=$(cd "$(dirname "$0")/../.." && pwd)
name="bianque-eval-$(openssl rand -hex 6)"
password=$(openssl rand -hex 16)

docker run --detach --rm --name "$name" --env "POSTGRES_PASSWORD=$password" \
  --publish 127.0.0.1::5432 pgvector/pgvector:pg16 >/dev/null
trap 'docker rm --force "$name" >/dev/null' EXIT
until docker exec "$name" pg_isready -U postgres >/dev/null 2>&1; do sleep 0.5; done
port=$(docker port "$name" 5432/tcp | head -n 1 | sed 's/.*://')

cd "$root/backend"
export BIANQUE_DATABASE_URL="postgresql+psycopg://postgres:$password@127.0.0.1:$port/postgres"
export BIANQUE_EMBEDDING_FIXTURE="$root/eval/embeddings/bge-small-en-v1.5.json"
# pg_isready answers before the server accepts TCP connections from the host.
for _ in $(seq 1 60); do
  uv run --locked alembic upgrade head >/dev/null 2>&1 && break
  sleep 0.5
done
uv run --locked alembic upgrade head
uv run --locked bianque knowledge ingest "$root/knowledge/runbooks"
uv run --locked bianque eval retrieval --queries "$root/eval/retrieval/queries.yaml" --out "$out"
echo "report: $out/retrieval.md"
