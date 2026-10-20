# The Bianque backend: HTTP API (and later the investigation runner and MCP server).
FROM python:3.12.14-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /bin/uv

# git: the Git backend of the change tools, and fast-import for evidence packs.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY alembic.ini ./
COPY migrations ./migrations
COPY bianque ./bianque
RUN uv sync --locked --no-dev

# The local embedding model is downloaded on first use into a cache volume at /models.
RUN useradd --create-home --uid 10001 bianque && mkdir /models && chown bianque /models
ENV PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/models
USER bianque

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn --factory bianque.api.app:create_app --host 0.0.0.0 --port 8000"]
