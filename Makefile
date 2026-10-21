.PHONY: check lint test frontend-check public-safety hooks demo demo-env demo-down demo-alerts inject-s1 \
	restore-s1 record-pack derive-packs helm-rbac replay-login replay replay-down e2e knowledge-fixtures \
	eval record

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

# The Helm chart lints, and the RBAC it renders with the default and the demo values only
# gets, lists and watches. Needs Helm, so it is not part of `make check`; CI runs it.
HELM_CHART := deploy/helm/bianque

helm-rbac:
	helm lint $(HELM_CHART)
	helm lint $(HELM_CHART) --values demo/bianque/values.yaml
	helm template bianque $(HELM_CHART) --namespace bianque | ($(UV) python -m tools.check_rbac)
	helm template bianque $(HELM_CHART) --namespace bianque --values demo/bianque/values.yaml | \
		($(UV) python -m tools.check_rbac)

# The demo profile: the environment (kind, Prometheus and Alertmanager, s1-app) and
# Bianque installed with its Helm chart, calling a live model. Needs LLM_API_KEY_FILE,
# the path of a file holding the key; the model defaults are those of
# `make replay LLM_MODE=live`.
demo: demo-env
	@test -n '$(LLM_API_KEY_FILE)' && test -r '$(LLM_API_KEY_FILE)' || \
		{ echo "make demo needs LLM_API_KEY_FILE, a readable file holding the key" >&2; exit 2; }
	LLM_API_KEY_FILE='$(LLM_API_KEY_FILE)' LLM_BASE_URL='$(LLM_BASE_URL)' LLM_MODEL='$(LLM_MODEL)' \
		LLM_EXTRA_BODY='$(LLM_EXTRA_BODY)' demo/scripts/install-bianque.sh

# The demo environment without Bianque, which recording evidence packs needs.
demo-env:
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
# it; the demo login, the MCP token and the A2A token are generated again on every run
# into .demo/replay-login.env, .demo/demo-password, .demo/mcp-token and .demo/a2a-token.
# None of them is ever committed.
REPLAY_ENV := .demo/replay.env
REPLAY_LOGIN_ENV := .demo/replay-login.env
DEMO_PASSWORD := .demo/demo-password
MCP_TOKEN := .demo/mcp-token
A2A_TOKEN := .demo/a2a-token
COMPOSE_FILES := docker compose -f deploy/compose/replay.yaml --env-file $(REPLAY_ENV)
COMPOSE_REPLAY := $(COMPOSE_FILES) --env-file $(REPLAY_LOGIN_ENV)
REPLAY_URL := http://127.0.0.1:$${BIANQUE_REPLAY_PORT:-8080}

$(REPLAY_ENV):
	mkdir -p .demo
	umask 077 && printf 'BIANQUE_DB_PASSWORD=%s\n' "$$(openssl rand -hex 16)" > $@

# The password is hashed inside the backend image, so the host needs no Python.
replay-login:
	mkdir -p .demo
	demo/scripts/demo-login.sh $(REPLAY_LOGIN_ENV) $(DEMO_PASSWORD) $(MCP_TOKEN) $(A2A_TOKEN) \
		docker run --rm -i "$$(docker build -q --target runtime --build-context skills=.agents/skills \
			backend)" bianque auth hash-password

# LLM_MODE picks the model mode: recorded (the default) replays recorded responses;
# scripted answers with the scripted rules; live calls a real model and needs
# LLM_API_KEY_FILE, the path of a file holding the key. The live defaults are DeepSeek's.
LLM_MODE ?= recorded
LLM_MODEL ?= deepseek-v4-pro
LLM_BASE_URL ?= https://api.deepseek.com
LLM_EXTRA_BODY ?= {"thinking": {"type": "disabled"}}
COMPOSE_MODE := $(COMPOSE_REPLAY) $(if $(filter live,$(LLM_MODE)),-f deploy/compose/live.yaml)

# The JSON-RPC request `make replay` prints: replay case s1-a as an A2A task, answered at
# once with the task; GetTask or SubscribeToTask follow it.
A2A_EXAMPLE := {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": \
	{"messageId": "1", "role": "ROLE_USER", "parts": [{"data": {"case_id": "s1-a"}}]}, \
	"configuration": {"returnImmediately": true}}}

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
	@echo "A2A: the Agent Card is $(REPLAY_URL)/.well-known/agent-card.json, the token is in $(A2A_TOKEN)."
	@echo "Replay a case as an A2A task, run from this directory:"
	@echo "  curl -s $(REPLAY_URL)/a2a -H \"Authorization: Bearer \$$(cat $(A2A_TOKEN))\" \\"
	@echo "    -H \"A2A-Version: 1.0\" -H \"Content-Type: application/json\" -d '"'$(A2A_EXAMPLE)'"'"

# Compose checks every variable of the file even to stop it; the login values are not
# used by `down`, so placeholders stand in when no login was generated yet.
replay-down:
	BIANQUE_DEMO_USERS=unused BIANQUE_SESSION_SECRET=unused BIANQUE_MCP_CLIENTS=unused \
		BIANQUE_A2A_CLIENTS=unused $(COMPOSE_FILES) down --volumes

# End-to-end tests: the replay stack in scripted model mode, which investigates every case
# once it has started, then Playwright against those investigations and new ones it
# triggers, then the A2A tests with the official A2A SDK's client (backend/e2e). Two A2A
# clients of their own replace the replay profile's: the env file given last wins.
# Screenshots land in frontend/e2e/screenshots. The stack starts from an empty database:
# decisions on suggestions are append-only and cannot be undone between runs.
E2E_A2A_ENV := .demo/e2e-a2a.env
E2E_A2A_TOKEN_A := .demo/e2e-a2a-token-a
E2E_A2A_TOKEN_B := .demo/e2e-a2a-token-b
COMPOSE_E2E := $(COMPOSE_REPLAY) --env-file $(E2E_A2A_ENV)

e2e: $(REPLAY_ENV) replay-login
	demo/scripts/e2e-a2a.sh $(E2E_A2A_ENV) $(E2E_A2A_TOKEN_A) $(E2E_A2A_TOKEN_B)
	$(COMPOSE_E2E) down --volumes
	BIANQUE_LLM_MODE=scripted $(COMPOSE_E2E) up --detach --build --wait
	cd frontend && BIANQUE_E2E_URL="$(REPLAY_URL)" BIANQUE_E2E_PASSWORD="$$(cat ../$(DEMO_PASSWORD))" \
		BIANQUE_E2E_A2A_TOKEN="$$(cat ../$(E2E_A2A_TOKEN_A))" npx playwright test
	cd backend && BIANQUE_E2E_URL="$(REPLAY_URL)" BIANQUE_E2E_A2A_TOKEN_A="$$(cat ../$(E2E_A2A_TOKEN_A))" \
		BIANQUE_E2E_A2A_TOKEN_B="$$(cat ../$(E2E_A2A_TOKEN_B))" uv run --locked pytest e2e

# Embedding fixture: vectors of every runbook chunk and evaluation query, computed with the
# local model. Rerun after editing knowledge/runbooks, eval/retrieval/queries.yaml or a
# case's alert or follow-up questions.
EMBEDDING_FIXTURE := ../eval/embeddings/bge-small-en-v1.5.json

knowledge-fixtures:
	$(UV) bianque knowledge download-model
	$(UV) bianque knowledge embed-fixtures --runbooks ../knowledge/runbooks --cases ../eval/cases \
		--queries ../eval/retrieval/queries.yaml --out $(EMBEDDING_FIXTURE)

# Evaluation: every case replayed with the recorded model responses and its questions
# asked, the retrieval evaluated, in a database and a process on an internal Docker
# network with no route out. The report lands in .demo/eval; the target
# fails when a hard gate fails. The report is copied out of the stopped container.
EVAL_OUT := .demo/eval
COMPOSE_EVAL := docker compose -f deploy/compose/eval.yaml

eval:
	$(COMPOSE_EVAL) down --volumes
	rm -rf $(EVAL_OUT) && mkdir -p .demo
	$(COMPOSE_EVAL) build
	status=0; $(COMPOSE_EVAL) run --name bianque-eval-run eval || status=$$?; \
	docker cp bianque-eval-run:/tmp/report $(EVAL_OUT) || true; \
	docker rm bianque-eval-run >/dev/null; $(COMPOSE_EVAL) down --volumes; \
	echo "report: $(EVAL_OUT)/report.md"; exit $$status

# Recording: every case investigated and asked by a real model, each response recorded,
# then replayed and compared before it replaces eval/recordings. Needs
# LLM_API_KEY_FILE; the model defaults are those of `make replay LLM_MODE=live`.
record:
	LLM_API_KEY_FILE='$(LLM_API_KEY_FILE)' LLM_MODEL='$(LLM_MODEL)' LLM_BASE_URL='$(LLM_BASE_URL)' \
		LLM_EXTRA_BODY='$(LLM_EXTRA_BODY)' backend/tools/record.sh .demo/record-$$(date +%Y%m%d-%H%M%S)
