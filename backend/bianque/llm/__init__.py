"""Model gateway: the single egress to the model service, with redaction enforced by type.

The package exposes the gateway (the single egress), the redaction types (the only way
to build gateway input), the shared value types and the errors. It deliberately does not
re-export any provider: an import edge from this module to ``bianque.llm.providers``
would add a second path to the model service. ``bianque/llm/gateway.py`` is the only
module allowed to import the provider layer (checked by
``tests/test_llm_egress_static.py``).
"""

from bianque.llm.errors import (
    LLMError,
    LLMRequestError,
    LLMUnavailableError,
    RecordingFormatError,
    RecordingNotFoundError,
    StructuredOutputError,
)
from bianque.llm.gateway import (
    CallRecorder,
    LLMGateway,
    RedactionResidueError,
    StructuredResult,
    build_gateway,
)
from bianque.llm.redacted import (
    MessageRole,
    RedactedMessage,
    RedactedPrompt,
    RedactedText,
    RedactedToolCall,
    assistant_message,
    build_redaction_engine,
    parse_internal_domain_suffixes,
    redact_for_llm,
    redact_prompt_for_llm,
    redact_tool_call,
    tool_result_message,
)
from bianque.llm.scripted import ScriptedReply, ScriptedRule
from bianque.llm.types import (
    CallContext,
    CallEvent,
    LLMCompletion,
    LLMUsage,
    MemoryCallRecorder,
    ToolCall,
    ToolChoice,
    ToolSpec,
)

__all__ = [
    "CallContext",
    "CallEvent",
    "CallRecorder",
    "LLMCompletion",
    "LLMError",
    "LLMGateway",
    "LLMRequestError",
    "LLMUnavailableError",
    "LLMUsage",
    "MemoryCallRecorder",
    "MessageRole",
    "RecordingFormatError",
    "RecordingNotFoundError",
    "RedactedMessage",
    "RedactedPrompt",
    "RedactedText",
    "RedactedToolCall",
    "RedactionResidueError",
    "ScriptedReply",
    "ScriptedRule",
    "StructuredOutputError",
    "StructuredResult",
    "ToolCall",
    "ToolChoice",
    "ToolSpec",
    "assistant_message",
    "build_gateway",
    "build_redaction_engine",
    "parse_internal_domain_suffixes",
    "redact_for_llm",
    "redact_prompt_for_llm",
    "redact_tool_call",
    "tool_result_message",
]
