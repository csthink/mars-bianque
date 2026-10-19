"""Fixture package that violates every import-linter contract on purpose.

One module per contract in ``pyproject.toml``:

- ``violation_openai.py`` imports the model SDK outside the provider layer;
- ``violation_fastapi.py`` imports FastAPI outside the API package.

The package is a separate root package outside ``bianque``, so the positive
configuration never sees it. ``contracts-negative.toml`` makes it the root package and
expects every mirrored contract to be BROKEN.
"""
