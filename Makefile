.PHONY: check lint test public-safety hooks demo demo-down demo-alerts inject-s1 restore-s1

UV := cd backend && uv run --locked

# Demo environment versions. The node image is the default of kind v0.33.0.
KIND_NODE_IMAGE := kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5
PROMETHEUS_CHART_VERSION := 29.37.0

check: lint test public-safety

lint:
	$(UV) ruff check . ../demo/s1-app
	$(UV) ruff format --check . ../demo/s1-app
	$(UV) mypy
	$(UV) lint-imports
	$(UV) python tools/check_env_prefix.py
	shellcheck demo/scripts/*.sh

test:
	$(UV) pytest

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
