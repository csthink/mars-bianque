"""bianque.redaction - the redaction engine (engine, patterns, entropy).

Public surface:
- ``redact(text)`` / ``detect(text)``: rewriting and detection are kept separate;
- ``RedactionEngine`` / ``build_engine(...)``: an engine configured with internal
  domain suffixes and entropy thresholds;
- ``Finding``: a detection hit, used by callers that only need to check text;
- ``generalize_private_ips(text)``: the private IP rule alone, for stored content.

``redact`` and ``detect`` take ``preserve``: identifiers (object names, commit hashes)
that the entropy fallback leaves intact so a later tool call can use them.
"""

from __future__ import annotations

from bianque.redaction.engine import (
    Finding,
    RedactionEngine,
    build_engine,
    detect,
    generalize_private_ips,
    redact,
)

__all__ = [
    "Finding",
    "RedactionEngine",
    "build_engine",
    "detect",
    "generalize_private_ips",
    "redact",
]
