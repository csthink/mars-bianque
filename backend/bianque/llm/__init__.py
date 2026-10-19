"""Model gateway: the single egress to the model service, with redaction enforced by type.

The package exposes only the public names of ``gateway`` (the single egress) and
``redacted`` (the only way to build redacted input). It deliberately does not re-export
any provider: an import edge from this module to ``bianque.llm.providers`` would add a
second path to the model service. ``bianque/llm/gateway.py`` is the only module allowed
to import the provider layer (checked by ``tests/test_llm_egress_static.py``).
"""

from bianque.llm.gateway import (
    LLMCompletion,
    LLMGateway,
    LLMUsage,
    RedactionResidueError,
    build_gateway,
)
from bianque.llm.redacted import (
    MessageRole,
    RedactedMessage,
    RedactedPrompt,
    RedactedText,
    build_redaction_engine,
    parse_internal_domain_suffixes,
    redact_for_llm,
    redact_prompt_for_llm,
)

__all__ = [
    "LLMCompletion",
    "LLMGateway",
    "LLMUsage",
    "MessageRole",
    "RedactedMessage",
    "RedactedPrompt",
    "RedactedText",
    "RedactionResidueError",
    "build_gateway",
    "build_redaction_engine",
    "parse_internal_domain_suffixes",
    "redact_for_llm",
    "redact_prompt_for_llm",
]
