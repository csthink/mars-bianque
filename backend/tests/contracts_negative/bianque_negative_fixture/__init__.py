"""Fixture package that violates every import-linter contract on purpose.

One module per contract in ``pyproject.toml``:

- ``violation_openai.py`` imports the model SDK outside the provider layer;
- ``violation_kubernetes.py`` imports the Kubernetes client outside the real backend;
- ``violation_fastembed.py`` imports the embedding library outside the embedding module;
- ``violation_gate_llm.py`` imports the model gateway, as the evidence gate must not;
- ``violation_mcp.py`` imports the MCP SDK outside the MCP package;
- ``violation_fastapi.py`` imports FastAPI outside the API package.

The package is a separate root package outside ``bianque``, so the positive
configuration never sees it. ``contracts-negative.toml`` makes it a root package and
expects every mirrored contract to be BROKEN.
"""
