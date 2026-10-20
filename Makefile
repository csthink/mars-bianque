.PHONY: check lint test frontend-check public-safety hooks demo demo-down demo-alerts inject-s1 \
	restore-s1 record-pack derive-packs replay-login replay replay-down e2e knowledge-fixtures \
	eval-retrieval

UV := cd backend && uv run --locked

# Demo environment versions. The node image is the default of kind v0.33.0.
KIND_NODE_IMAGE := kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5
PROMETHEUS_CHART_VERSION := 29.37.0

check: lint test frontend-check public-safety

lint:
	$(UV) ruff check . ../demo/s1-app
	$(UV) ruff format --check . ../demo/s1-app
	$(UV) mypy
	$(UV) lint-imports
	$(UV) python tools/check_env_prefix.py
	$(UV) python tools/check_k8s_readonly.py
	shellcheck demo/scripts/*.sh backend/tools/*.sh

test:
	$(UV) pytest

frontend-check:
	cd frontend && npm run lint && npm run typecheck && npm test && npx vite build

public-safety:
	tools/check-public-safety.sh --selftest
	tools/check-public-safety.sh

hooks:
	tools/install-hooks.sh

demo:
	KIND_NODE_IMAGE='$(KIND_NODE_IMAGE)' PROMETHEUS_CHART_VERSION='$(PROMETHEUS_CHART_VERSION)' \
		demo/scripts/up.sh

demo-down:
	demo/scripts/down.sh

demo-alerts:
	demo/scripts/alerts.sh

inject-s1:
	demo/scripts/inject-s1.sh '$(CASE)'

restore-s1:
	demo/scripts/restore-s1.sh

record-pack:
	PROMETHEUS_CHART_VERSION='$(PROMETHEUS_CHART_VERSION)' demo/scripts/record-pack.sh '$(CASE)'

# S1-d and S1-f are derived from the S1-a pack: a log tool timeout during replay, and a
# pack that keeps only the alert and the pods.
derive-packs:
	$(UV) bianque pack derive --from ../eval/cases/s1-a/pack --out ../eval/cases/s1-d/pack \
		--case s1-d --fault logs_get_container_logs:timeout
	$(UV) bianque pack derive --from ../eval/cases/s1-a/pack --out ../eval/cases/s1-f/pack \
		--case s1-f --only alert,pods

# Replay profile: database, backend and nginx in Docker Compose, no cluster needed. The
# database password is generated once into .demo/replay.env, since the data volume keeps
# it; the demo login is generated again on every run into .demo/replay-login.env and
# .demo/demo-password. None of them is ever committed.
REPLAY_ENV := .demo/replay.env
REPLAY_LOGIN_ENV := .demo/replay-login.env
DEMO_PASSWORD := .demo/demo-password
COMPOSE_FILES := docker compose -f deploy/compose/replay.yaml --env-file $(REPLAY_ENV)
COMPOSE_REPLAY := $(COMPOSE_FILES) --env-file $(REPLAY_LOGIN_ENV)
COMPOSE_E2E := $(COMPOSE_REPLAY) -f deploy/compose/e2e.yaml
REPLAY_URL := http://127.0.0.1:$${BIANQUE_REPLAY_PORT:-8080}

$(REPLAY_ENV):
	mkdir -p .demo
	umask 077 && printf 'BIANQUE_DB_PASSWORD=%s\n' "$$(openssl rand -hex 16)" > $@

# The password is hashed inside the backend image, so the host needs no Python.
replay-login:
	mkdir -p .demo
	demo/scripts/demo-login.sh $(REPLAY_LOGIN_ENV) $(DEMO_PASSWORD) \
		docker run --rm -i "$$(docker build -q backend)" bianque auth hash-password

replay: $(REPLAY_ENV) replay-login
	$(COMPOSE_REPLAY) up --detach --build --wait
	@echo "Bianque (replay profile): $(REPLAY_URL)"
	@echo "Demo login: demo / $$(cat $(DEMO_PASSWORD))"

# Compose checks every variable of the file even to stop it; the login values are not
# used by `down`, so placeholders stand in when no login was generated yet.
replay-down:
	BIANQUE_DEMO_USERS=unused BIANQUE_SESSION_SECRET=unused $(COMPOSE_FILES) down --volumes

# End-to-end tests: the replay stack with the database exposed to the host, the
# investigation fixture loaded, then Playwright. Screenshots land in frontend/e2e/screenshots.
# The stack starts from an empty database: decisions on suggestions are append-only, so
# a fixture investigation that was decided on in an earlier run cannot be replaced.
e2e: $(REPLAY_ENV) replay-login
	$(COMPOSE_E2E) down --volumes
	$(COMPOSE_E2E) up --detach --build --wait
	. ./$(REPLAY_ENV) && cd backend && uv run --locked python tests/api/investigation_fixture.py \
		--database-url "postgresql+psycopg://postgres:$$BIANQUE_DB_PASSWORD@127.0.0.1:$${BIANQUE_E2E_DB_PORT:-55432}/postgres"
	cd frontend && BIANQUE_E2E_URL="$(REPLAY_URL)" npx playwright test

# Embedding fixture: vectors of every runbook chunk and evaluation query, computed with the
# local model. Rerun after editing knowledge/runbooks, eval/retrieval/queries.yaml or a
# case's alert or follow-up questions.
EMBEDDING_FIXTURE := ../eval/embeddings/bge-small-en-v1.5.json

knowledge-fixtures:
	$(UV) bianque knowledge download-model
	$(UV) bianque knowledge embed-fixtures --runbooks ../knowledge/runbooks --cases ../eval/cases \
		--queries ../eval/retrieval/queries.yaml --out $(EMBEDDING_FIXTURE)

# Retrieval evaluation in a throwaway database; the report lands in .demo/eval.
eval-retrieval:
	backend/tools/eval_retrieval.sh .demo/eval
