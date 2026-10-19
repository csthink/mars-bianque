"""High-entropy string detection.

Fallback layer behind the rule set: for every run of 20 or more base64/hex characters,
compute the Shannon entropy and treat values above the threshold (default 3.5
bits/char) as a likely key or digest. Rules run first (``patterns``); this layer only
catches long random tokens the rules missed. Entropy alone is not used for redaction,
because it would also mask diagnosable structure.

**Exemption for diagnosable structure**: long hash-shaped values such as image digests
and trace ids are useful for diagnosis and must keep their structure rather than be
masked. Before the entropy scan, these explicitly labelled forms are identified and
exempted:
  - **Algorithm-prefixed digest** ``<algo>:<hexdigest>``, where algo is one of sha1,
    sha224, sha256, sha384, sha512, sha512_256, md5, blake2b, blake2s, and the hex
    length equals that algorithm's digest length. This is the OCI/docker image digest
    and checksum form; the ``sha256:`` prefix marks it as a digest, not a key.
  - **W3C trace context**: the ``traceparent`` header form
    (``00-<32hex>-<16hex>-<2hex>``) and labelled trace/span ids (``trace_id=`` /
    ``trace-id:`` / ``span_id=`` followed by 16-32 hex characters).
Only these labelled forms are exempted. Bare hex is not: an unlabelled long hex string
may still be a hex-encoded key and is masked by the entropy fallback.

The exemption covers **only the diagnostic span itself**. When a high-entropy candidate
overlaps a diagnostic span, the span is subtracted from the candidate and each
remaining piece is checked on its own (see ``iter_high_entropy_spans``). So
``sha256:<64hex>`` and ``trace_id=<32hex>`` keep their structure, while a high-entropy
secret glued before or after them (with ``_`` or ``-``) is still masked and still
reported by ``detect()``. A secret cannot hide behind a digest.

Detection and rewriting are separate: this module only exposes detection primitives
(is this high-entropy, and where are the tokens). Rewriting into placeholders is done
in ``engine``.
"""

from __future__ import annotations

import math
import re
from collections import Counter

#: Minimum length of a high-entropy candidate token. Shorter tokens are never checked,
#: so ordinary identifiers and short hashes are not misclassified.
MIN_HIGH_ENTROPY_LEN = 20

#: Default Shannon entropy threshold in bits/char. Values above it count as high entropy.
DEFAULT_ENTROPY_THRESHOLD = 3.5

#: Character set of a high-entropy candidate: base64 including the URL-safe ``-`` and
#: ``_``; hex is a subset. A candidate is a maximal run of at least
#: MIN_HIGH_ENTROPY_LEN such characters. ``\b`` is not a reliable boundary because base64
#: contains the non-word characters ``+ / =``, so boundaries are defined explicitly as
#: "preceded and followed by a non-candidate character".
_TOKEN_CHARS = r"A-Za-z0-9+/=_-"
_CANDIDATE_RE = re.compile(
    rf"(?<![{_TOKEN_CHARS}])[{_TOKEN_CHARS}]{{{MIN_HIGH_ENTROPY_LEN},}}(?![{_TOKEN_CHARS}])"
)

#: Digest algorithm -> canonical hex digest length. ``<algo>:<hex>`` counts as a digest
#: only when the hex length is **exactly** the canonical length, so an algorithm name
#: followed by a random hex key of some other length is not exempted.
_DIGEST_HEXLEN = {
    "md5": 32,
    "sha1": 40,
    "sha224": 56,
    "sha256": 64,
    "sha384": 96,
    "sha512": 128,
    "sha512_256": 64,
    "blake2b": 128,
    "blake2s": 64,
}
#: Longer algorithm names first (sha512_256 before sha512) so a shorter name does not
#: match a prefix of a longer one.
_DIGEST_ALGOS_ALT = "|".join(sorted(_DIGEST_HEXLEN, key=len, reverse=True))
#: ``<algo>:<hex>``; the lookbehind and lookahead reject adjacent alphanumerics. The
#: length check happens afterwards.
_ALGO_DIGEST_RE = re.compile(
    rf"(?<![A-Za-z0-9])(?P<algo>{_DIGEST_ALGOS_ALT}):(?P<hex>[0-9a-fA-F]+)(?![0-9A-Za-z])",
    re.IGNORECASE,
)
#: W3C traceparent header: ``00-<32hex>-<16hex>-<2hex>`` (version-traceid-spanid-flags).
_TRACEPARENT_RE = re.compile(
    r"(?<![0-9A-Za-z])00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}(?![0-9A-Za-z])",
    re.IGNORECASE,
)
#: Labelled trace/span id: ``trace_id=`` / ``trace-id:`` / ``span_id=`` + 16-32 hex.
_LABELED_TRACE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:trace|span)[_-]?id\s*[=:]\s*(?P<hex>[0-9a-fA-F]{16,32})(?![0-9A-Za-z])",
    re.IGNORECASE,
)


def iter_diagnostic_structure_spans(text: str) -> list[tuple[int, int]]:
    """Return ``(start, end)`` of every diagnosable structure that must be preserved.

    Covers algorithm-prefixed digests and W3C trace ids. The entropy fallback does not
    mask these spans. Each span covers the whole structure, including the ``<algo>:``
    prefix or the trace label, which simplifies overlap checks.
    """
    spans: list[tuple[int, int]] = []
    for m in _ALGO_DIGEST_RE.finditer(text):
        if len(m.group("hex")) == _DIGEST_HEXLEN[m.group("algo").lower()]:
            spans.append((m.start(), m.end()))
    for m in _TRACEPARENT_RE.finditer(text):
        spans.append((m.start(), m.end()))
    for m in _LABELED_TRACE_RE.finditer(text):
        spans.append((m.start(), m.end()))
    return spans


def _subtract_spans(start: int, end: int, cut: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Remove every overlapping diagnostic ``cut`` span from ``[start, end)``.

    The exemption applies only to the diagnostic span itself, not to high-entropy text
    glued before or after it. In ``sha256:<64hex>_<secret>``, skipping the whole
    candidate because it overlaps the digest would leak ``_<secret>`` and make
    ``detect()`` miss it. Instead this computes the interval difference: the remaining
    non-diagnostic pieces go back to the entropy check (see the caller), the diagnostic
    structure is kept, and a glued secret is still masked. Intervals are half-open; the
    result is sorted by start and non-overlapping.
    """
    pieces = [(start, end)]
    for cs, ce in cut:
        nxt: list[tuple[int, int]] = []
        for ps, pe in pieces:
            if ce <= ps or cs >= pe:  # no overlap: keep the whole piece
                nxt.append((ps, pe))
                continue
            if ps < cs:  # piece to the left of the diagnostic span
                nxt.append((ps, cs))
            if ce < pe:  # piece to the right of the diagnostic span
                nxt.append((ce, pe))
        pieces = nxt
    return pieces


def shannon_entropy(s: str) -> float:
    """Character-level Shannon entropy in bits/char; 0 for the empty string."""
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def is_high_entropy(
    token: str,
    *,
    threshold: float = DEFAULT_ENTROPY_THRESHOLD,
    min_len: int = MIN_HIGH_ENTROPY_LEN,
) -> bool:
    """Return whether a token is long enough and its Shannon entropy exceeds threshold."""
    return len(token) >= min_len and shannon_entropy(token) > threshold


def iter_high_entropy_spans(
    text: str,
    *,
    threshold: float = DEFAULT_ENTROPY_THRESHOLD,
    min_len: int = MIN_HIGH_ENTROPY_LEN,
) -> list[tuple[int, int, str]]:
    """Scan text and return ``(start, end, token)`` for every high-entropy token.

    Detection only, no rewriting. A candidate is a maximal run of base64/hex characters
    of length at least ``min_len``. **Order matters: subtract diagnostic spans first,
    then check entropy on the remaining pieces.** Each candidate has the diagnosable
    structures (see ``iter_diagnostic_structure_spans``) subtracted from it, so only
    the diagnostic span itself is exempt; each remaining piece is checked for length and
    entropy on its own, and qualifying pieces are reported. An image digest or trace id
    keeps its structure, but a high-entropy secret glued to it is not exempted along
    with it.

    **Do not pre-filter the whole merged candidate by entropy before subtracting
    spans.** When the diagnostic structure itself has low entropy (e.g. ``sha256:``
    followed by 64 ``a`` characters, or a traceparent of repeated hex), it pulls the
    entropy of "diagnostic span + glued high-entropy secret" below the threshold. A
    pre-filter would then skip the candidate, the subtraction would never run, and the
    secret would leak while ``detect()`` returned nothing. So the entropy of the whole
    candidate takes no part in any decision; only the pieces left after subtraction are
    checked.

    A bare long hex string (no diagnostic label) does not overlap any diagnostic span,
    so the difference is the string itself and it is checked and masked as usual.
    Returned spans are sorted by start and non-overlapping, ready for in-place
    replacement.
    """
    diagnostic = iter_diagnostic_structure_spans(text)
    spans: list[tuple[int, int, str]] = []
    for m in _CANDIDATE_RE.finditer(text):
        for seg_start, seg_end in _subtract_spans(m.start(), m.end(), diagnostic):
            # A piece may start or end with the glue separator (_ or -). Strip it before
            # checking so the span covers exactly the secret: the separator stays out of
            # the placeholder and the head/tail characters are clean.
            seg = text[seg_start:seg_end]
            stripped = seg.strip("_-")
            if not stripped:
                continue
            off = seg.index(stripped)
            s0, e0 = seg_start + off, seg_start + off + len(stripped)
            if is_high_entropy(stripped, threshold=threshold, min_len=min_len):
                spans.append((s0, e0, stripped))
    return spans
