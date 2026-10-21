# The Bianque backend: HTTP API, investigation runner and MCP server.
#
# Two stages: `runtime` has no embedding model and serves the replay profile, the
# evaluation and the end-to-end tests, which use the embedding fixture; `demo`, the
# default, adds the local embedding model for the demo profile. Building `demo` downloads
# the model once, so CI builds `runtime` and never reaches the model host.
FROM python:3.12.14-slim-bookworm AS runtime

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

# The local embedding model's directory. The model is never downloaded at run time: the
# `demo` stage downloads it here at build time; on `runtime`, `local` embedding mode
# needs `bianque knowledge download-model` run into a volume mounted here first.
RUN useradd --create-home --uid 10001 bianque && mkdir /models && chown bianque /models
ENV PATH="/app/.venv/bin:$PATH" \
    BIANQUE_EMBEDDING_MODEL_DIR=/models
USER bianque

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn --factory bianque.api.app:create_app --host 0.0.0.0 --port 8000"]

# The demo image: the runtime with the embedding model, its SHA-256 checked on download.
FROM runtime AS demo
RUN bianque knowledge download-model
