"""Identifiers of persisted entities.

Investigation IDs are ULIDs, so they sort by creation time and never enter a prompt. So
are the IDs of A2A tasks and of the contexts Bianque opens for them.

Evidence, suggestion and knowledge IDs are derived from content. Evidence and
suggestion IDs appear in prompts, and recorded model responses are looked up by a key
computed from the prompt, so these IDs must not depend on time or on the order of
execution: the same content always yields the same ID. As a consequence an evidence
or suggestion ID is unique only within one investigation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from ulid import ULID

INVESTIGATION_PREFIX = "inv_"
A2A_TASK_PREFIX = "task_"
A2A_CONTEXT_PREFIX = "ctx_"
EVIDENCE_PREFIX = "ev_"
SUGGESTION_PREFIX = "sg_"
KNOWLEDGE_PREFIX = "kn_"

_EVIDENCE_HEX = 10
_SUGGESTION_HEX = 10
_KNOWLEDGE_HEX = 12

#: Source name used for evidence that comes from the alert itself.
ALERT_SOURCE = "alertmanager"


def _digest(fields: Sequence[Any], length: int) -> str:
    """Hash a list of fields into a hex prefix.

    The fields are serialized as one canonical JSON array (sorted keys, no extra
    whitespace, UTF-8), so field boundaries are unambiguous: ``["ab", "c"]`` and
    ``["a", "bc"]`` hash differently.

    Args:
        fields: JSON-serializable values.
        length: Number of hex characters to keep.

    Returns:
        The first ``length`` hex characters of the SHA-256 digest.
    """
    canonical = json.dumps(list(fields), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:length]


def new_investigation_id() -> str:
    """Return a new investigation ID: ``inv_`` followed by a 26-character ULID."""
    return f"{INVESTIGATION_PREFIX}{ULID()}"


def new_a2a_task_id() -> str:
    """Return a new A2A task ID: ``task_`` followed by a 26-character ULID."""
    return f"{A2A_TASK_PREFIX}{ULID()}"


def new_a2a_context_id() -> str:
    """Return a new A2A context ID: ``ctx_`` followed by a 26-character ULID."""
    return f"{A2A_CONTEXT_PREFIX}{ULID()}"


def evidence_id(tool: str, identity_params: Mapping[str, Any], redacted_text: str) -> str:
    """Return the ID of evidence collected by a tool.

    Args:
        tool: Tool name.
        identity_params: The parameters that identify what the tool read (for
            example namespace and object name). Their order does not matter.
        redacted_text: The redacted tool result.

    Returns:
        ``ev_`` followed by 10 hex characters.
    """
    return EVIDENCE_PREFIX + _digest([tool, dict(identity_params), redacted_text], _EVIDENCE_HEX)


def alert_evidence_id(fingerprint: str, redacted_text: str) -> str:
    """Return the ID of the evidence that records the alert itself.

    Args:
        fingerprint: The Alertmanager fingerprint.
        redacted_text: The redacted alert text.

    Returns:
        ``ev_`` followed by 10 hex characters.
    """
    return EVIDENCE_PREFIX + _digest([ALERT_SOURCE, fingerprint, redacted_text], _EVIDENCE_HEX)


def suggestion_id(
    seq: int, action: str, command: str | None, risk: str, citations: Sequence[str]
) -> str:
    """Return the ID of a remediation suggestion.

    Args:
        seq: Position of the suggestion in the report.
        action: What the suggestion proposes.
        command: The command it proposes, if any.
        risk: ``read_only`` or ``change``.
        citations: Evidence IDs it cites. Their order does not matter.

    Returns:
        ``sg_`` followed by 10 hex characters.
    """
    fields = [seq, action, command, risk, sorted(citations)]
    return SUGGESTION_PREFIX + _digest(fields, _SUGGESTION_HEX)


def knowledge_id(path: str, heading_path: str, ordinal: int) -> str:
    """Return the ID of a knowledge chunk.

    Importing the same document again yields the same IDs.

    Args:
        path: Path of the runbook file, relative to the knowledge root.
        heading_path: The headings above the chunk, joined with `` > ``.
        ordinal: Position of the chunk in the document.

    Returns:
        ``kn_`` followed by 12 hex characters.
    """
    return KNOWLEDGE_PREFIX + _digest([path, heading_path, ordinal], _KNOWLEDGE_HEX)
