"""Process-wide settings: the single entry point for reading the environment.

Conventions (checked by ``tools/check_env_prefix.py``):

- Every environment variable owned by Bianque carries the ``BIANQUE_`` prefix.
- The environment is read only here. pydantic-settings applies ``env_prefix`` so that
  each field maps to ``BIANQUE_<FIELD>``; no other module reads ``os.environ``.
  Code elsewhere obtains values through ``get_settings()`` and receives them as
  arguments.
"""

from __future__ import annotations

from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

#: The one environment variable prefix in the repository. ``check_env_prefix.py``
#: asserts this value statically.
ENV_PREFIX = "BIANQUE_"

#: Where investigation tools read from. ``live`` queries a real cluster; ``pack``
#: reads a recorded evidence pack and needs no external service.
ToolBackend = Literal["live", "pack"]

#: How the model gateway produces completions. ``live`` calls an OpenAI-compatible
#: endpoint; ``recorded`` replays recorded responses; ``scripted`` returns
#: deterministic responses without any network access.
LLMMode = Literal["live", "recorded", "scripted"]

#: How embeddings are produced. ``local`` runs a local embedding model; ``fixture``
#: reads precomputed vectors. Embeddings never go through the model service.
EmbeddingMode = Literal["local", "fixture"]


class Settings(BaseSettings):
    """Process settings. Each field maps to the ``BIANQUE_<FIELD>`` environment variable.

    The prefix is enforced by ``model_config``; fields must not declare their own
    environment variable names or aliases.

    The defaults of the three mode fields form the set that needs no external
    service and no credential.
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        extra="ignore",
        frozen=True,
    )

    #: Tool backend (``BIANQUE_TOOL_BACKEND``).
    tool_backend: ToolBackend = "pack"

    #: Model gateway mode (``BIANQUE_LLM_MODE``).
    llm_mode: LLMMode = "scripted"

    #: Embedding mode (``BIANQUE_EMBEDDING_MODE``).
    embedding_mode: EmbeddingMode = "fixture"

    #: PostgreSQL connection URL (``BIANQUE_DATABASE_URL``).
    database_url: str | None = None

    #: OpenAI-compatible endpoint (``BIANQUE_LLM_BASE_URL``). ``None`` uses the SDK
    #: default endpoint.
    llm_base_url: str | None = None

    #: Chat model name (``BIANQUE_LLM_MODEL``). Required in ``live`` mode.
    llm_model: str = ""

    #: Model service credential (``BIANQUE_LLM_API_KEY``). The value is either the key
    #: itself or a reference ``file:<absolute path>`` to a file holding the key. It is
    #: stored as ``SecretStr`` so that it never appears in ``repr``, ``str`` or logs.
    llm_api_key: SecretStr | None = None

    #: Timeout in seconds for one model call (``BIANQUE_LLM_TIMEOUT_SECONDS``).
    llm_timeout_seconds: int = 60

    #: Token budget of one investigation (``BIANQUE_INVESTIGATION_TOKEN_BUDGET``).
    investigation_token_budget: int = 120000

    #: Comma-separated internal domain suffixes (``BIANQUE_REDACT_INTERNAL_DOMAINS``).
    #: The redaction engine generalizes host names under these suffixes; when empty,
    #: the engine has no internal domain rule.
    redact_internal_domains: str = ""


def get_settings() -> Settings:
    """Build a settings instance from the current environment.

    Returns:
        A frozen settings instance.
    """
    return Settings()
