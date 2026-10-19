.PHONY: check lint test public-safety hooks

UV := cd backend && uv run --locked

check: lint test public-safety

lint:
	$(UV) ruff check .
	$(UV) ruff format --check .
	$(UV) mypy
	$(UV) lint-imports
	$(UV) python tools/check_env_prefix.py

test:
	$(UV) pytest

public-safety:
	tools/check-public-safety.sh --selftest
	tools/check-public-safety.sh

hooks:
	tools/install-hooks.sh
