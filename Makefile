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
# it; the demo login and the MCP token are generated again on every run into
# .demo/replay-login.env, .demo/demo-password and .demo/mcp-token. None of them is ever
# committed.
REPLAY_ENV := .demo/replay.env
REPLAY_LOGIN_ENV := .demo/replay-login.env
DEMO_PASSWORD := .demo/demo-password
MCP_TOKEN := .demo/mcp-token
COMPOSE_FILES := docker compose -f deploy/compose/replay.yaml --env-file $(REPLAY_ENV)
COMPOSE_REPLAY := $(COMPOSE_FILES) --env-file $(REPLAY_LOGIN_ENV)
REPLAY_URL := http://127.0.0.1:$${BIANQUE_REPLAY_PORT:-8080}

$(REPLAY_ENV):
	mkdir -p .demo
	umask 077 && printf 'BIANQUE_DB_PASSWORD=%s\n' "$$(openssl rand -hex 16)" > $@

# The password is hashed inside the backend image, so the host needs no Python.
replay-login:
	mkdir -p .demo
	demo/scripts/demo-login.sh $(REPLAY_LOGIN_ENV) $(DEMO_PASSWORD) $(MCP_TOKEN) \
		docker run --rm -i "$$(docker build -q backend)" bianque auth hash-password

# LLM_MODE picks the model mode: recorded (the default) replays recorded responses;
# scripted answers with the scripted rules; live calls a real model and needs
# LLM_API_KEY_FILE, the path of a file holding the key. The live defaults are DeepSeek's.
LLM_MODE ?= recorded
LLM_MODEL ?= deepseek-v4-pro
LLM_BASE_URL ?= https://api.deepseek.com
LLM_EXTRA_BODY ?= {"thinking": {"type": "disabled"}}
COMPOSE_MODE := $(COMPOSE_REPLAY) $(if $(filter live,$(LLM_MODE)),-f deploy/compose/live.yaml)

replay: $(REPLAY_ENV) replay-login
	@case '$(LLM_MODE)' in recorded|scripted|live) ;; \
		*) echo "LLM_MODE must be recorded, scripted or live" >&2; exit 2 ;; esac
ifeq ($(LLM_MODE),live)
	@test -n '$(LLM_API_KEY_FILE)' && test -r '$(LLM_API_KEY_FILE)' || \
		{ echo "LLM_MODE=live needs LLM_API_KEY_FILE, a readable file holding the key" >&2; exit 2; }
endif
	export BIANQUE_LLM_MODE='$(LLM_MODE)' LLM_API_KEY_FILE='$(LLM_API_KEY_FILE)' \
		LLM_MODEL='$(LLM_MODEL)' LLM_BASE_URL='$(LLM_BASE_URL)' LLM_EXTRA_BODY='$(LLM_EXTRA_BODY)' && \
	$(COMPOSE_MODE) build && \
	$(if $(filter live,$(LLM_MODE)),$(COMPOSE_MODE) run --rm --no-deps backend \
		bianque knowledge download-model &&) \
	$(COMPOSE_MODE) up --detach --wait
	@echo "Bianque (replay profile, $(LLM_MODE) model mode): $(REPLAY_URL)"
	@echo "Demo login: demo / $$(cat $(DEMO_PASSWORD))"
	@echo "MCP for Claude Code, run from this directory:"
	@echo "  claude mcp add --transport http bianque $(REPLAY_URL)/mcp --header \"Authorization: Bearer \$$(cat $(MCP_TOKEN))\""

# Compose checks every variable of the file even to stop it; the login values are not
# used by `down`, so placeholders stand in when no login was generated yet.
replay-down:
	BIANQUE_DEMO_USERS=unused BIANQUE_SESSION_SECRET=unused BIANQUE_MCP_CLIENTS=unused \
		$(COMPOSE_FILES) down --volumes

# End-to-end tests: the replay stack in scripted model mode, which investigates every case
# once it has started, then Playwright against those investigations and new ones it
# triggers. Screenshots land in frontend/e2e/screenshots. The stack starts from an empty
# database: decisions on suggestions are append-only and cannot be undone between runs.
e2e: $(REPLAY_ENV) replay-login
	$(COMPOSE_REPLAY) down --volumes
	BIANQUE_LLM_MODE=scripted $(COMPOSE_REPLAY) up --detach --build --wait
	cd frontend && BIANQUE_E2E_URL="$(REPLAY_URL)" BIANQUE_E2E_PASSWORD="$$(cat ../$(DEMO_PASSWORD))" \
		npx playwright test

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
