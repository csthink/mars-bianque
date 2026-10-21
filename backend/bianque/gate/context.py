"""What the validators know about the investigation an output belongs to."""

from __future__ import annotations

from dataclasses import dataclass, field

from bianque.redaction import RedactionEngine


@dataclass(frozen=True)
class FailedCall:
    """An evidence tool call of the investigation that failed.

    Attributes:
        tool: The tool name.
        domain: The evidence domain of the tool.
        error: The error category, such as ``timeout`` or ``not_found``.
        caps_confidence: Whether the failure rules out a ``high`` root cause confidence;
            taken from the evidence domain declaration.
    """

    tool: str
    domain: str
    error: str
    caps_confidence: bool


@dataclass(frozen=True)
class GateContext:
    """The investigation data an output is checked against.

    Attributes:
        evidence_ids: The evidence IDs of the investigation.
        knowledge_ids: The knowledge IDs given to the model.
        suggestion_ids: The suggestion IDs of the published report (follow-up answers).
        preserved: The identifiers of all evidence of the investigation; they are not
            redaction residue.
        failed_calls: The failed evidence tool calls of the investigation.
        engine: The redaction engine configured with the internal domain suffixes.
        skill_names: The names of the skills; a skill is neither evidence nor knowledge.
    """

    evidence_ids: frozenset[str] = frozenset()
    knowledge_ids: frozenset[str] = frozenset()
    suggestion_ids: frozenset[str] = frozenset()
    preserved: frozenset[str] = frozenset()
    failed_calls: tuple[FailedCall, ...] = ()
    engine: RedactionEngine = field(default_factory=RedactionEngine)
    skill_names: frozenset[str] = frozenset()
