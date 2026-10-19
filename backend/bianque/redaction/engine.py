"""Redaction engine.

``redact()`` is the only rewriting entry point. It is separate from the read-only
``detect()``, which output validators use to check text without changing it. This
module, together with ``patterns`` and ``entropy``, guarantees three properties:

- **Coverage**: the rule set (``patterns.build_rules``) plus a high-entropy fallback
  (``entropy``).
- **Structure preservation**: a private IPv4 address keeps its first two octets
  (``10.12.x.x``), internal domains become ``*.internal``, and high-entropy tokens
  become ``<high-entropy:8a…3f>`` (the first and last 2 characters are kept for
  cross-reference).
- **Idempotence** ``redact(redact(x)) == redact(x)``: no placeholder form triggers any
  rule or the entropy check again (placeholders contain no quoted key-value pair, no run
  of 20+ base64 characters, and no RFC 1918 address).

Configuration boundary: reading the environment (``BIANQUE_REDACT_INTERNAL_DOMAINS``)
belongs to the config layer. This module only accepts explicit internal domain suffixes
and threshold parameters and never reads ``os.environ``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from bianque.redaction.entropy import (
    DEFAULT_ENTROPY_THRESHOLD,
    MIN_HIGH_ENTROPY_LEN,
    iter_high_entropy_spans,
)
from bianque.redaction.patterns import Rule, build_rules

#: Number of leading and trailing characters a high-entropy placeholder keeps
#: (``<high-entropy:8a…3f>``).
_HIGH_ENTROPY_HEAD_TAIL = 2

#: Matches an already generated high-entropy placeholder so a second pass can skip it.
#: The base64 run inside a placeholder is picked up by the entropy candidate regex and
#: must be excluded explicitly, otherwise a second ``redact`` would wrap it again.
_HIGH_ENTROPY_PLACEHOLDER_RE = re.compile(r"<high-entropy:[^>]*>")


def _high_entropy_placeholder(token: str) -> str:
    head = token[:_HIGH_ENTROPY_HEAD_TAIL]
    tail = token[-_HIGH_ENTROPY_HEAD_TAIL:]
    return f"<high-entropy:{head}…{tail}>"


@dataclass(frozen=True)
class RedactionEngine:
    """Deterministic redaction engine: the same input always gives the same output.

    Constructor parameters:
    - ``internal_domain_suffixes``: internal domain suffixes, injected by the deployment
      through the config layer;
    - ``entropy_threshold`` / ``min_high_entropy_len``: thresholds for the high-entropy
      fallback.
    """

    internal_domain_suffixes: tuple[str, ...] = ()
    entropy_threshold: float = DEFAULT_ENTROPY_THRESHOLD
    min_high_entropy_len: int = MIN_HIGH_ENTROPY_LEN
    _rules: tuple[Rule, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Compile the rule set from internal_domain_suffixes into the frozen _rules."""
        object.__setattr__(self, "_rules", build_rules(self.internal_domain_suffixes))

    # -- Rewriting -------------------------------------------------------------
    def redact(self, text: str) -> str:
        """Mask sensitive content while keeping diagnosable structure; idempotent.

        Pipeline: apply each rule in order (order is priority), then run the
        high-entropy fallback over the remaining long tokens. Each rule scans the text
        once, and its placeholders contain no triggering form, so rules do not stack on
        each other or on a second call.
        """
        if not text:
            return text
        for rule in self._rules:
            text = rule.regex.sub(rule.rewrite, text)
        text = self._redact_high_entropy(text)
        return text

    def _redact_high_entropy(self, text: str) -> str:
        spans = iter_high_entropy_spans(
            text,
            threshold=self.entropy_threshold,
            min_len=self.min_high_entropy_len,
        )
        if not spans:
            return text
        out: list[str] = []
        cursor = 0
        for start, end, token in spans:
            # Skip hits inside an existing high-entropy placeholder (idempotence).
            if _is_within_existing_placeholder(text, start, end):
                continue
            out.append(text[cursor:start])
            out.append(_high_entropy_placeholder(token))
            cursor = end
        out.append(text[cursor:])
        return "".join(out)

    # -- Detection (read-only, no rewriting) -----------------------------------
    def detect(self, text: str) -> list[Finding]:
        """Locate every sensitive hit without rewriting.

        A non-empty result means the text still contains sensitive content. Uses the
        same rules and entropy settings as ``redact``, so an output check can reject on
        any hit. For redacted text ``detect(redact(x))`` is empty: a placeholder may be
        matched by a rule again, but its rewrite is a no-op (identical to the matched
        text) and is filtered out, so it is not reported as leftover content.
        """
        findings: list[Finding] = []
        for rule in self._rules:
            for m in rule.finditer(text):
                # A hit whose rewrite is a no-op is not leftover sensitive content
                # (same criterion as the rewriting side):
                #   1. the private IP rule matching a public IP, which is kept as is;
                #   2. a placeholder in already redacted text matched again
                #      (e.g. secret=<redacted:secret>), whose rewrite equals the match.
                # This makes detect(redact(x)) empty, so an output check does not
                # reject text that has already been redacted.
                if rule.rewrite(m) == m.group(0):
                    continue
                findings.append(Finding(rule.name, m.start(), m.end(), m.group(0)))
        for start, end, token in iter_high_entropy_spans(
            text,
            threshold=self.entropy_threshold,
            min_len=self.min_high_entropy_len,
        ):
            if _is_within_existing_placeholder(text, start, end):
                continue
            findings.append(Finding("high-entropy", start, end, token))
        return findings


@dataclass(frozen=True)
class Finding:
    """One detection hit (detection output only, no rewrite)."""

    rule: str
    start: int
    end: int
    matched: str


def _is_within_existing_placeholder(text: str, start: int, end: int) -> bool:
    """Return whether ``[start, end)`` lies inside an existing ``<high-entropy:...>``."""
    for pm in _HIGH_ENTROPY_PLACEHOLDER_RE.finditer(text):
        if pm.start() <= start and end <= pm.end():
            return True
    return False


#: Process-wide default engine with no internal domain suffixes (deployments inject
#: suffixes through the config layer). Pure and stateless, so it can be shared.
_DEFAULT_ENGINE = RedactionEngine()


def redact(text: str, *, engine: RedactionEngine | None = None) -> str:
    """Module-level rewriting helper.

    Uses the default engine with no internal domain suffixes; pass a configured
    ``engine`` when suffixes are needed.
    """
    return (engine or _DEFAULT_ENGINE).redact(text)


def detect(text: str, *, engine: RedactionEngine | None = None) -> list[Finding]:
    """Module-level detection helper (read-only)."""
    return (engine or _DEFAULT_ENGINE).detect(text)


def build_engine(
    internal_domain_suffixes: Iterable[str] = (),
    *,
    entropy_threshold: float = DEFAULT_ENTROPY_THRESHOLD,
    min_high_entropy_len: int = MIN_HIGH_ENTROPY_LEN,
) -> RedactionEngine:
    """Build an engine from deployment configuration (the config layer calls this)."""
    return RedactionEngine(
        internal_domain_suffixes=tuple(internal_domain_suffixes),
        entropy_threshold=entropy_threshold,
        min_high_entropy_len=min_high_entropy_len,
    )
