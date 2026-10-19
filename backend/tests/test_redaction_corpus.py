"""Regression tests for the redaction engine.

Covers:
1. Every sensitive class in the positive corpus is masked, and diagnosable structure in
   the negative corpus is left unchanged (fixed corpus, run in CI);
2. Private IPs are generalized with their structure kept (first two octets + ``x.x``);
3. Idempotence: redact(redact(x)) == redact(x);
4. High-entropy tokens (20+ base64/hex characters) are masked with their first and last
   characters kept for cross-reference.

The corpus lives in fixtures/redaction/{positive,negative}.json next to this file.
Synthetic secrets in the corpus and in this file are assembled at runtime from
fragments, so no complete secret literal appears in the source.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bianque.redaction import Finding, build_engine, detect, redact
from bianque.redaction.engine import RedactionEngine
from bianque.redaction.entropy import (
    MIN_HIGH_ENTROPY_LEN,
    is_high_entropy,
    shannon_entropy,
)
from bianque.redaction.patterns import PLACEHOLDER_PREFIX

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "redaction"

#: Placeholder in a corpus ``input`` that the loader replaces with the joined secret.
_SECRET_SLOT = "{secret}"

# Every class must have at least one case in both the positive and negative corpus.
_REQUIRED_CLASSES = {
    "secret",
    "token",
    "password",
    "api-key",
    "authorization",
    "cookie",
    "dsn",
    "private-ip",
    "internal-domain",
    "high-entropy",
}


def _load(name: str) -> list[dict[str, Any]]:
    """Load a corpus file and resolve each case's ``secret_parts`` into its input.

    A case with ``secret_parts`` gets ``raw_secret`` set to the joined fragments, and
    ``{secret}`` in its ``input`` replaced by that value.
    """
    data = json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))
    cases: list[dict[str, Any]] = []
    for case in data["cases"]:
        parts: list[str] | None = case.pop("secret_parts", None)
        if parts is not None:
            secret = "".join(parts)
            case["raw_secret"] = secret
            case["input"] = case["input"].replace(_SECRET_SLOT, secret)
        cases.append(case)
    return cases


_POSITIVE = _load("positive.json")
_NEGATIVE = _load("negative.json")


def _engine_for(case: dict[str, Any]) -> RedactionEngine:
    return build_engine(case.get("internal_domain_suffixes", []))


# -- Positive corpus: every class is masked ----------------------------------------
def test_positive_corpus_covers_every_class() -> None:
    classes = {c["class"] for c in _POSITIVE}
    assert _REQUIRED_CLASSES <= classes, (
        f"positive corpus is missing classes: {_REQUIRED_CLASSES - classes}"
    )


@pytest.mark.parametrize("case", _POSITIVE, ids=[c["class"] for c in _POSITIVE])
def test_positive_samples_are_redacted(case: dict[str, Any]) -> None:
    engine = _engine_for(case)
    out = engine.redact(case["input"])
    raw = case["raw_secret"]
    # The original sensitive value must not remain...
    assert raw not in out, f"{case['class']}: {raw!r} was not masked -> {out!r}"
    # ...and masking actually happened (output differs and carries a redaction marker).
    assert out != case["input"], f"{case['class']}: nothing was masked"
    marks = (PLACEHOLDER_PREFIX, "<high-entropy:", ".x.x", "*.internal")
    assert any(mark in out for mark in marks)


# -- Negative corpus: diagnosable structure is left unchanged ----------------------
def test_negative_corpus_covers_every_class() -> None:
    classes = {c["class"] for c in _NEGATIVE}
    assert _REQUIRED_CLASSES <= classes, (
        f"negative corpus is missing classes: {_REQUIRED_CLASSES - classes}"
    )


@pytest.mark.parametrize("case", _NEGATIVE, ids=[c["class"] for c in _NEGATIVE])
def test_negative_samples_are_untouched(case: dict[str, Any]) -> None:
    engine = _engine_for(case)
    out = engine.redact(case["input"])
    assert out == case["must_equal"], (
        f"{case['class']}: diagnosable structure was altered {case['input']!r} -> {out!r}"
    )


# -- Private IPs are generalized with structure kept -------------------------------
def test_private_ip_generalized_preserving_structure() -> None:
    assert redact("node " + "10.12" + ".34.56 down") == "node 10.12.x.x down"
    # All three RFC 1918 ranges keep their first two octets.
    assert redact("172.16" + ".5.9") == "172.16.x.x"
    assert redact("192.168" + ".1.254") == "192.168.x.x"
    assert redact("10.0" + ".0.1") == "10.0.x.x"


def test_public_ip_not_touched() -> None:
    # Public IPs are useful for diagnosis and must be kept (not generalized).
    assert redact("LB 203.0.113.7 up") == "LB 203.0.113.7 up"
    assert redact("dns 8.8.8.8 ok") == "dns 8.8.8.8 ok"
    # 172.15 and 172.32 are outside the RFC 1918 range 172.16-31 and are not generalized.
    assert redact("host 172.15.0.1") == "host 172.15.0.1"
    assert redact("host 172.32.0.1") == "host 172.32.0.1"


# -- Idempotence --------------------------------------------------------------------
@pytest.mark.parametrize(
    "case", _POSITIVE + _NEGATIVE, ids=[f"{c['class']}" for c in _POSITIVE + _NEGATIVE]
)
def test_idempotent_on_corpus(case: dict[str, Any]) -> None:
    engine = _engine_for(case)
    once = engine.redact(case["input"])
    twice = engine.redact(once)
    assert once == twice, f"{case['class']}: not idempotent {once!r} -> {twice!r}"


def test_idempotent_on_mixed_multiline() -> None:
    # Several classes across several lines: the first and second pass agree.
    engine = build_engine(["corp.internal"])
    password = "hunter2" + "Passw0rd!"
    dsn_password = "p4" + "ss"
    blob = "9f8e7d6c5b4a3021" + "ffeeddccbbaa9988"
    node_ip = "10.12" + ".34.56"
    text = (
        f"password={password} host={node_ip}\n"
        f"dsn postgres://u:{dsn_password}@db:5432/x svc api.corp.internal\n"
        f"blob {blob} done"
    )
    once = engine.redact(text)
    assert once == engine.redact(once)
    # No original sensitive value remains.
    for leaked in (password, dsn_password, blob):
        assert leaked not in once
    assert "10.12.x.x" in once
    assert "*.internal" in once


# -- High-entropy tokens are masked with head and tail kept ------------------------
def test_high_entropy_masked_with_head_tail() -> None:
    hex_value = "9f8e7d6c5b4a3021" + "ffeeddccbbaa9988"  # 32 hex, high entropy
    out = redact(f"digest {hex_value} end")
    assert hex_value not in out
    # The first and last 2 characters are kept (<high-entropy:8a…3f>).
    assert f"<high-entropy:{hex_value[:2]}…{hex_value[-2:]}>" in out


def test_high_entropy_base64_masked() -> None:
    b64_value = "QUJDREVGR0hJSktM" + "TU5PUFFSU1RVVldY"  # 32 base64 chars
    out = redact(f"secret_blob {b64_value} tail")
    assert b64_value not in out
    assert f"<high-entropy:{b64_value[:2]}…{b64_value[-2:]}>" in out


def test_short_or_low_entropy_not_high_entropy() -> None:
    # Shorter than the minimum length: never checked for entropy.
    assert not is_high_entropy("abc123")
    # Long enough but low entropy (repeated character): not high entropy.
    assert not is_high_entropy("a" * (MIN_HIGH_ENTROPY_LEN + 5))
    # Random hex has higher entropy than a repeated character.
    assert shannon_entropy("9f8e7d6c5b4a3021ffee") > shannon_entropy("a" * 20)


# -- Detection and rewriting are separate: detect is read-only, empty after redact --
@pytest.mark.parametrize("case", _POSITIVE, ids=[c["class"] for c in _POSITIVE])
def test_detect_finds_then_redact_clears(case: dict[str, Any]) -> None:
    engine = _engine_for(case)
    # Detection: the original text has hits.
    findings = engine.detect(case["input"])
    assert findings, f"{case['class']}: detect found no sensitive content"
    assert all(isinstance(f, Finding) for f in findings)
    # Detection after rewriting: nothing left (the detection side of idempotence).
    assert engine.detect(engine.redact(case["input"])) == []


def test_module_level_helpers_use_default_engine() -> None:
    # The module-level helpers behave like an engine with no internal domain suffixes.
    s = "token=" + "ghp_" + "ABCDEFabcdef0123456789ABCDEFabcd" + " x"
    assert redact(s) == RedactionEngine().redact(s)
    assert [f.rule for f in detect(s)] == [f.rule for f in RedactionEngine().detect(s)]


# -- DSN password containing a bare @ is fully masked, and detect agrees -----------
# Credentials must be masked up to the last @ (the userinfo boundary), not cut at the
# first @ leaving the tail; and detect(redact(x)) must be empty.
_DSN_AT_PASSWORD = "pa" + "@ss"
_DSN_AT = "postgres://user:" + _DSN_AT_PASSWORD + "@db.internal:5432/app"


def test_dsn_password_with_raw_at_fully_redacted() -> None:
    out = redact(_DSN_AT)
    # No part of the password may remain (especially the ss@ tail after the first @).
    assert _DSN_AT_PASSWORD not in out
    assert "ss@db" not in out
    assert out == "postgres://<redacted:dsn-credential>@db.internal:5432/app"
    # The host is kept (diagnosable endpoint); only the credentials are masked.
    assert "db.internal:5432/app" in out


def test_dsn_multi_at_password_fully_redacted() -> None:
    # Several @ in the password: still masked up to the last @ before the host.
    password = "p@w" + "@rd"
    s = "mongodb://u:" + password + "@mongo0:27017/db"
    out = redact(s)
    assert password not in out and "w@rd" not in out
    assert out == "mongodb://<redacted:dsn-credential>@mongo0:27017/db"


def test_dsn_raw_at_no_false_green_on_detect() -> None:
    # Output validation rejects on any detect hit. The original must be reported, and
    # the masked output must have no hits: a partial mask that leaves a tail while
    # detect(out) is empty would pass validation with a leak.
    s = _DSN_AT
    assert any(f.rule == "dsn" for f in detect(s)), (
        "the original contains unmasked credentials; detect must report them"
    )
    assert detect(redact(s)) == [], "masked output must have no sensitive content left"


def test_dsn_no_credential_uri_untouched() -> None:
    # A URI without user:pass@ is a diagnosable endpoint and is not masked.
    s = "healthcheck hits http://svc:8080/healthz with no creds"
    assert redact(s) == s


# -- The high-entropy fallback keeps diagnosable structure --------------------------
# A full sha256 digest or a trace id exceeds the entropy threshold but must keep its
# structure (to identify the image or link traces), so the fallback must not mask it.
def test_full_sha256_digest_preserved() -> None:
    # A real 64-hex sha256 digest (entropy > 3.5); the algorithm prefix marks it as
    # diagnosable structure, so it is kept whole.
    digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert is_high_entropy(digest), (
        "the digest must exceed the entropy threshold for this test to be meaningful"
    )
    s = f"image digest sha256:{digest} pulled"
    assert redact(s) == s
    # Detection agrees: diagnosable structure is not reported as sensitive.
    assert detect(s) == []


def test_full_sha512_digest_preserved() -> None:
    digest = (
        "cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce"
        "47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e"
    )  # sha512 of the empty string, 128 hex
    s = f"blob sha512:{digest} verified"
    assert redact(s) == s


def test_labeled_trace_id_preserved() -> None:
    # A labelled trace id (32 hex, above the entropy threshold) is diagnosable structure.
    trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    assert is_high_entropy(trace)
    s = f"req trace_id={trace} handled"
    assert redact(s) == s
    assert detect(s) == []


def test_traceparent_header_preserved() -> None:
    # A W3C traceparent header is kept whole (version-traceid-spanid-flags).
    s = "hdr traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01 ok"
    assert redact(s) == s


def test_bare_hex_secret_still_masked() -> None:
    # Only labelled diagnostic forms are exempt. A bare long hex string with no
    # algorithm prefix or trace label may be a hex-encoded key and is still masked.
    hex_value = "9f8e7d6c5b4a3021" + "ffeeddccbbaa9988" + "abcdef0123456789"
    assert is_high_entropy(hex_value)
    out = redact(f"leaked {hex_value} here")
    assert hex_value not in out
    assert f"<high-entropy:{hex_value[:2]}…{hex_value[-2:]}>" in out


def test_algo_prefixed_but_wrong_length_still_masked() -> None:
    # "algo:hex" whose length differs from the canonical digest length is not a real
    # digest (it may be a coincidental prefix plus a key), so it is not exempt and is
    # masked when high-entropy. Here: sha256: prefix with high-entropy hex that is not
    # 64 characters long.
    blob = "a1b2c3d4e5f6a7b8c9" + "d0e1f2a3b4c5d6e7f8"  # 36 hex, matches no algorithm
    assert is_high_entropy(blob)
    out = redact(f"weird sha256:{blob} x")
    assert blob not in out
    assert "<high-entropy:" in out


# -- The exemption covers only the diagnostic span; a glued secret is still masked --
# A high-entropy secret glued with _ or - after (or before) a sha256 or trace structure
# forms one candidate that overlaps the diagnostic span. Exempting the whole candidate
# would leak the secret and make detect() miss it. The candidate minus the diagnostic
# span is checked piece by piece instead: the structure is kept, the secret is masked,
# and detect(redact(x)) == [].
_GLUED_HEX = "9f8e7d6c5b4a3021" + "ffeeddccbbaa9988" + "abcdef0123456789"  # 48 hex
#: Leading part of the digest: if a high-entropy finding contains it, the digest itself
#: was wrongly reported.
_DIGEST_HEX_PREFIX = "e3b0c44298fc1c149afbf4c8996fb924"


def test_digest_glued_suffix_secret_masked_structure_preserved() -> None:
    # sha256:<64hex>_<secret>: digest kept, glued secret masked, detect agrees.
    digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert is_high_entropy(_GLUED_HEX)
    s = f"artifact sha256:{digest}_{_GLUED_HEX} tail"
    out = redact(s)
    assert _GLUED_HEX not in out, f"glued secret leaked: {out!r}"
    # The diagnostic structure (algorithm-prefixed digest) is kept as is.
    assert f"sha256:{digest}" in out
    # The secret becomes a high-entropy placeholder (head and tail kept).
    assert f"<high-entropy:{_GLUED_HEX[:2]}…{_GLUED_HEX[-2:]}>" in out
    # Nothing left after masking, and idempotent.
    assert detect(out) == [], f"detect still reports hits: {detect(out)!r}"
    assert redact(out) == out


def test_trace_id_glued_suffix_secret_masked_structure_preserved() -> None:
    # trace_id=<32hex>-<secret>: trace kept, glued secret masked, detect agrees.
    trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    s = f"trace_id={trace}-{_GLUED_HEX} end"
    out = redact(s)
    assert _GLUED_HEX not in out, f"glued secret leaked: {out!r}"
    assert f"trace_id={trace}" in out
    assert f"<high-entropy:{_GLUED_HEX[:2]}…{_GLUED_HEX[-2:]}>" in out
    assert detect(out) == [], f"detect still reports hits: {detect(out)!r}"
    assert redact(out) == out


def test_digest_glued_prefix_secret_masked_structure_preserved() -> None:
    # Secret glued **before** the digest (<secret>_sha256:<64hex>): the interval
    # difference is symmetric, so the secret is still masked.
    digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    s = f"x {_GLUED_HEX}_sha256:{digest} y"
    out = redact(s)
    assert _GLUED_HEX not in out, f"secret glued before the digest leaked: {out!r}"
    assert f"sha256:{digest}" in out
    assert f"<high-entropy:{_GLUED_HEX[:2]}…{_GLUED_HEX[-2:]}>" in out
    assert detect(out) == []
    assert redact(out) == out


def test_glued_secret_detect_not_false_green_on_raw() -> None:
    # Output validation rejects on any detect hit. Original text with an unmasked glued
    # secret must be reported (if the whole candidate were exempted, detect on the
    # original would also be empty), and detect must be empty after masking.
    digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    s = f"artifact sha256:{digest}_{_GLUED_HEX} tail"
    findings = detect(s)
    assert findings, "text with an unmasked glued secret must be reported by detect"
    assert any(f.rule == "high-entropy" for f in findings)
    # The hit is the secret piece, not the exempt digest.
    assert all(_DIGEST_HEX_PREFIX not in f.matched for f in findings if f.rule == "high-entropy")
    assert detect(redact(s)) == []


# -- A low-entropy diagnostic span must not block the check of a glued secret -------
# If the whole merged candidate were pre-filtered by entropy before subtracting the
# diagnostic span, a low-entropy structure (sha256: followed by 64 a characters, or a
# traceparent of repeated hex) glued with _ or - to a high-entropy secret would pull the
# merged candidate below the threshold. The pre-filter would skip it, the subtraction
# would never run, the secret would leak, and detect() would return nothing. The entropy
# of the whole candidate therefore takes no part in filtering: diagnostic spans are
# subtracted first and each remaining piece is checked
# (entropy.iter_high_entropy_spans).
_LOW_ENTROPY_DIGEST_HEX = "a" * 64  # valid sha256 digest length, but one repeated char
_LOW_ENTROPY_TRACEPARENT = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"


@pytest.mark.parametrize("glue", ["_", "-"], ids=["underscore", "hyphen"])
def test_low_entropy_digest_glued_secret_masked(glue: str) -> None:
    # Precondition: the merged candidate (low-entropy digest hex + glued secret) is
    # below the entropy threshold as a whole; otherwise a pre-filter would let it
    # through and this test would not cover the pre-filter path.
    merged = f"{_LOW_ENTROPY_DIGEST_HEX}{glue}{_GLUED_HEX}"
    assert not is_high_entropy(merged), (
        "the merged candidate must be low entropy to cover the pre-filter path"
    )
    assert is_high_entropy(_GLUED_HEX)
    s = f"artifact sha256:{_LOW_ENTROPY_DIGEST_HEX}{glue}{_GLUED_HEX} tail"
    out = redact(s)
    assert _GLUED_HEX not in out, f"glued secret leaked: {out!r}"
    # The diagnostic structure (low entropy, but a valid digest length) is kept as is.
    assert f"sha256:{_LOW_ENTROPY_DIGEST_HEX}" in out
    assert f"<high-entropy:{_GLUED_HEX[:2]}…{_GLUED_HEX[-2:]}>" in out
    # Detection agrees: the original is reported, the masked output is clean, and
    # redaction is idempotent.
    findings = detect(s)
    assert findings, "text with an unmasked glued secret must be reported by detect"
    assert any(f.rule == "high-entropy" for f in findings)
    assert detect(out) == [], f"detect still reports hits: {detect(out)!r}"
    assert redact(out) == out


@pytest.mark.parametrize("glue", ["_", "-"], ids=["underscore", "hyphen"])
def test_low_entropy_traceparent_glued_secret_masked(glue: str) -> None:
    # traceparent variant: a high-entropy secret glued after a low-entropy W3C trace
    # context.
    merged = f"{_LOW_ENTROPY_TRACEPARENT}{glue}{_GLUED_HEX}"
    assert not is_high_entropy(merged), (
        "the merged candidate must be low entropy to cover the pre-filter path"
    )
    s = f"hdr traceparent: {_LOW_ENTROPY_TRACEPARENT}{glue}{_GLUED_HEX} end"
    out = redact(s)
    assert _GLUED_HEX not in out, f"glued secret leaked: {out!r}"
    # The traceparent structure is kept as is.
    assert _LOW_ENTROPY_TRACEPARENT in out
    assert f"<high-entropy:{_GLUED_HEX[:2]}…{_GLUED_HEX[-2:]}>" in out
    findings = detect(s)
    assert findings, "text with an unmasked glued secret must be reported by detect"
    assert any(f.rule == "high-entropy" for f in findings)
    assert detect(out) == [], f"detect still reports hits: {detect(out)!r}"
    assert redact(out) == out


def test_low_entropy_diagnostic_alone_still_preserved() -> None:
    # Control: a low-entropy diagnostic structure with no glued secret is left
    # unchanged and detect is empty. Subtracting spans first does not pull the
    # structure itself or its neighbours into a mask.
    s1 = f"image digest sha256:{_LOW_ENTROPY_DIGEST_HEX} pulled"
    assert redact(s1) == s1
    assert detect(s1) == []
    s2 = f"hdr traceparent: {_LOW_ENTROPY_TRACEPARENT} ok"
    assert redact(s2) == s2
    assert detect(s2) == []


# -- JSON: a quoted key before the separator, and key=value inside a JSON string ---
def test_json_key_value_pair_is_redacted_and_detected() -> None:
    value = "Js0n" + "Passw0rd!"
    text = json.dumps({"user": "appuser", "pass" + "word": value})
    assert detect(text)
    out = redact(text)
    assert value not in out
    assert detect(out) == []
    assert json.loads(out)["user"] == "appuser"


def test_key_value_inside_json_string_keeps_json_valid() -> None:
    value = "abc" + "123def456"
    text = json.dumps({"command": "curl -H tok" + f"en={value} https://example.com"})
    out = redact(text)
    assert value not in out
    assert json.loads(out)["command"].startswith("curl -H token=")
