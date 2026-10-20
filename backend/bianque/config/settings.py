"""Process-wide settings: the single entry point for reading the environment.

Conventions (checked by ``tools/check_env_prefix.py``):

- Every environment variable owned by Bianque carries the ``BIANQUE_`` prefix.
- The environment is read only here. pydantic-settings applies ``env_prefix`` so that
  each field maps to ``BIANQUE_<FIELD>``; no other module reads ``os.environ``.
  Code elsewhere obtains values through ``get_settings()`` and receives them as
  arguments.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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

    #: Model gateway mode (``BIANQUE_LLM_MODE``). ``scripted`` must be set explicitly.
    llm_mode: LLMMode = "recorded"

    #: Embedding mode (``BIANQUE_EMBEDDING_MODE``).
    embedding_mode: EmbeddingMode = "fixture"

    #: Directory of the local embedding model's files (``BIANQUE_EMBEDDING_MODEL_DIR``),
    #: filled by ``bianque knowledge download-model``; the model never downloads at run
    #: time.
    embedding_model_dir: Path = Path.home() / ".cache" / "bianque" / "models"

    #: The embedding fixture read in ``fixture`` mode (``BIANQUE_EMBEDDING_FIXTURE``).
    embedding_fixture: Path = Path("eval/embeddings/bge-small-en-v1.5.json")

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

    #: Retries after a timeout, connection failure, HTTP 429 or 5xx
    #: (``BIANQUE_LLM_MAX_RETRIES``).
    llm_max_retries: int = 2

    #: Provider-specific request body fields as a JSON object
    #: (``BIANQUE_LLM_EXTRA_BODY``), for example ``{"thinking": {"type": "disabled"}}``
    #: for a provider whose default thinking mode rejects a forced tool choice.
    llm_extra_body: Annotated[dict[str, Any], NoDecode] = {}

    #: Directory searched for recorded responses in ``recorded`` mode
    #: (``BIANQUE_LLM_RECORDINGS_DIR``).
    llm_recordings_dir: Path = Path("eval/recordings")

    #: When set in ``live`` mode, every successful call is recorded into this directory
    #: (``BIANQUE_LLM_RECORD_DIR``).
    llm_record_dir: Path | None = None

    #: Token budget of one investigation (``BIANQUE_INVESTIGATION_TOKEN_BUDGET``). It is
    #: checked once after each round of evidence collection; once exceeded, the
    #: investigation stops replanning and writes its report.
    investigation_token_budget: int = 120000

    #: Model turns of one step of an evidence sub-agent (``BIANQUE_SUBAGENT_MAX_STEPS``).
    #: At the limit the sub-agent must submit its findings.
    subagent_max_steps: int = Field(default=6, ge=1)

    #: Supplementary plans of one investigation (``BIANQUE_MAX_REPLAN_ROUNDS``).
    max_replan_rounds: int = Field(default=2, ge=0)

    #: Retries after the evidence gate rejects a report (``BIANQUE_REPORT_MAX_RETRIES``);
    #: when they are used up, a degraded report is published.
    report_max_retries: int = Field(default=2, ge=0)

    #: Kubeconfig context of the real tool backend (``BIANQUE_KUBE_CONTEXT``). Empty
    #: inside a cluster, where the service account is used; on a workstation it names
    #: the cluster explicitly, so the host's current context never decides what is read.
    kube_context: str | None = None

    #: Path of the manifest Git repository read by the real tool backend
    #: (``BIANQUE_GIT_REPO_PATH``).
    git_repo_path: Path | None = None

    #: Directory holding the evaluation cases and their evidence packs
    #: (``BIANQUE_CASES_DIR``); each case keeps its pack in ``<case>/pack``.
    cases_dir: Path = Path("eval/cases")

    #: The case whose evidence pack the replay backend reads when no case is given
    #: (``BIANQUE_PACK_CASE``).
    pack_case: str = "s1-a"

    #: Comma-separated internal domain suffixes (``BIANQUE_REDACT_INTERNAL_DOMAINS``).
    #: The redaction engine generalizes host names under these suffixes; when empty,
    #: the engine has no internal domain rule.
    redact_internal_domains: str = ""

    @field_validator("llm_extra_body", mode="before")
    @classmethod
    def _parse_extra_body(cls, value: Any) -> Any:
        """Accept the JSON text of an environment variable; an empty value means none."""
        if isinstance(value, str):
            return json.loads(value) if value.strip() else {}
        return value


def get_settings() -> Settings:
    """Build a settings instance from the current environment.

    Returns:
        A frozen settings instance.
    """
    return Settings()
