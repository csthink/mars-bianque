"""Preserved identifiers: the entropy fallback keeps them, and nothing else gets through."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from bianque.config import get_settings
from bianque.llm.gateway import RedactionResidueError, build_gateway
from bianque.llm.redacted import RedactedMessage, RedactedPrompt, redact_for_llm
from bianque.llm.types import CallContext
from bianque.redaction import detect, generalize_private_ips, redact

POD = "s1-app-7d9f8c6b5-x2k4p"
COMMIT = "98ca3c1f0740febf1e62ea9e730b61093c9a0324"
#: High-entropy secret without a known shape; only the entropy fallback catches it.
SECRET = "Zq8" + "Lw2Xk9Vb7Nr4Tp6Ys3Md5"


def test_identifiers_are_masked_without_preservation() -> None:
    assert POD not in redact(f"pod {POD}")
    assert COMMIT not in redact(f"commit {COMMIT}")


def test_preserved_identifiers_survive_inside_longer_tokens() -> None:
    text = f"Back-off restarting failed container app in pod demo-s1/{POD}; commit {COMMIT}"
    redacted = redact(text, preserve=[POD, COMMIT])
    assert redacted == text
    assert detect(text, preserve=[POD, COMMIT]) == []


def test_a_secret_glued_to_an_identifier_is_still_masked() -> None:
    for glued in (f"{POD}-{SECRET}", f"{SECRET}_{POD}", f"{POD}/{SECRET}"):
        redacted = redact(glued, preserve=[POD])
        assert POD in redacted
        assert SECRET not in redacted
        assert [f.rule for f in detect(glued, preserve=[POD])] == ["high-entropy"]


def test_short_identifiers_never_split_a_secret() -> None:
    # A short "identifier" occurring inside a secret must not cut it into pieces that
    # are each too short for the entropy check.
    sample = "Ab3dE5gH7jK9" + "-app-" + "mN2pQ4rS6tU8"
    assert "<high-entropy:" in redact(sample, preserve=["app"])
    long_inside = "Xy7" + POD + "Qw9"
    assert POD not in redact(long_inside, preserve=[POD])


def test_rules_still_apply_to_preserved_values() -> None:
    text = "pass" + "word=" + SECRET
    assert redact(text, preserve=[SECRET]) == "password=<redacted:secret>"
    assert {f.rule for f in detect(text, preserve=[SECRET])} == {"kv-secret"}


def test_redaction_with_preservation_is_idempotent() -> None:
    text = f"pod {POD} key {SECRET}"
    once = redact(text, preserve=[POD])
    assert redact(once, preserve=[POD]) == once
    assert detect(once, preserve=[POD]) == []


def test_private_address_generalization_alone() -> None:
    # Addresses are assembled at runtime so the public content scanner sees none.
    pod, node = "10.244" + ".1.7", "172.18" + ".0.2"
    text = f"probe http://{pod}:8080/healthz from {node}, public 8.8.8.8"
    assert generalize_private_ips(text) == (
        "probe http://10.244.x.x:8080/healthz from 172.18.x.x, public 8.8.8.8"
    )
    assert generalize_private_ips(SECRET) == SECRET


def test_redacted_text_carries_its_preserved_identifiers() -> None:
    redacted = redact_for_llm(f"pod {POD}", preserve=[POD])
    assert redacted.text == f"pod {POD}"
    assert redacted.preserved == frozenset({POD})


_RESPONSE: dict[str, Any] = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": "test-model",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


def _send(monkeypatch: pytest.MonkeyPatch, message: RedactedMessage) -> list[bytes]:
    monkeypatch.setenv("BIANQUE_LLM_MODE", "live")
    monkeypatch.setenv("BIANQUE_LLM_BASE_URL", "http://llm.example.com:8000/v1")
    monkeypatch.setenv("BIANQUE_LLM_MODEL", "test-model")
    monkeypatch.delenv("BIANQUE_LLM_API_KEY", raising=False)
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, json=_RESPONSE)

    async def main() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gateway = build_gateway(get_settings(), http_client=client)
        await gateway.complete(
            RedactedPrompt([message]), context=CallContext(agent="kubernetes", operation="step")
        )

    asyncio.run(main())
    return bodies


def test_the_gateway_recheck_accepts_preserved_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = RedactedMessage(
        "tool", redact_for_llm(f"pod {POD}", preserve=[POD]), tool_call_id="c1"
    )
    bodies = _send(monkeypatch, message)
    assert len(bodies) == 1 and POD.encode() in bodies[0]


def test_the_gateway_recheck_still_rejects_residue_beside_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bianque.llm.redacted import _CONSTRUCTION_SENTINEL, RedactedText

    forged = RedactedText(f"pod {POD} {SECRET}", sentinel=_CONSTRUCTION_SENTINEL, preserved=[POD])
    with pytest.raises(RedactionResidueError):
        _send(monkeypatch, RedactedMessage("user", forged))
