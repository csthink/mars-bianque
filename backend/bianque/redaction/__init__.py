"""bianque.redaction - the redaction engine (engine, patterns, entropy).

Public surface:
- ``redact(text)`` / ``detect(text)``: rewriting and detection are kept separate;
- ``RedactionEngine`` / ``build_engine(...)``: an engine configured with internal
  domain suffixes and entropy thresholds;
- ``Finding``: a detection hit, used by callers that only need to check text.
"""

from __future__ import annotations

from bianque.redaction.engine import (
    Finding,
    RedactionEngine,
    build_engine,
    detect,
    redact,
)

__all__ = [
    "Finding",
    "RedactionEngine",
    "build_engine",
    "detect",
    "redact",
]
