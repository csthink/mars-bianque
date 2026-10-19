"""Runtime gate and pre-send recheck of the model gateway.

Four groups of assertions:

- **No plaintext in the request body**: five kinds of input (log, event, configuration,
  diff, user message) each carry a known secret, and so do the arguments of a tool call
  in an assistant message and the result in a tool message. After going through the
  redaction pipeline and the gateway, the raw request bytes are intercepted and none of
  the secrets may appear.
- **Runtime gate**: passing a plain ``str``, a bare list of messages, or a prompt whose
  message content or tool call arguments are not ``RedactedText`` raises ``TypeError``
  with nothing sent. This covers dynamic construction and deserialization, which bypass
  static type checking.
- **Pre-send recheck**: a ``RedactedText`` forged with the construction sentinel while
  still holding a secret, as a message content or as tool call arguments, is caught by
  ``detect()`` before sending, with nothing sent.
- **Public surface regression**: starting from the gateway's public attributes, no
  reachable object can send requests directly (provider, HTTP or SDK client,
  transport). The gates live in ``LLMGateway._require_redacted()`` and
  ``LLMGateway._recheck()``; once the public surface hands
  out an object that holds a client, one line such as
  ``gateway.<attribute>.<client>.chat.completions.create(...)`` sends a raw string past
  every gate.

All secrets are synthetic values assembled at runtime, not real credentials.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest

from bianque.config import get_settings
from bianque.llm import CallContext, ToolCall
from bianque.llm.gateway import (
    LLMGateway,
    RedactionResidueError,
    build_gateway,
)
from bianque.llm.redacted import (
    _CONSTRUCTION_SENTINEL,
    RedactedMessage,
    RedactedPrompt,
    RedactedText,
    RedactedToolCall,
    assistant_message,
    redact_for_llm,
    tool_result_message,
)

# Synthetic secrets, assembled from fragments so that no complete secret literal
# appears in the source.
_PASSWORD = "hunter2" + "Passw0rd!"
_GITHUB_TOKEN = "gh" + "p_" + "ABCDEFabcdef0123456789" + "ABCDEFabcd"
_CONFIG_SECRET = "Sup3r" + "SecretV@lue"
_DSN_PASSWORD = "pgP4" + "ssw0rd"
_CLOUD_KEY = "AK" + "IA" + "IOSFODNN7" + "EXAMPLE"

#: Five kinds of input: (kind, raw text, the secret inside it).
_ENTRY_SAMPLES: tuple[tuple[str, str, str], ...] = (
    ("log", "log line: pass" + "word: '" + _PASSWORD + "' accepted", _PASSWORD),
    ("event", "event payload tok" + "en=" + _GITHUB_TOKEN + " emitted", _GITHUB_TOKEN),
    (
        "config",
        "config: db_sec" + "ret=" + _CONFIG_SECRET + " and rest of line",
        _CONFIG_SECRET,
    ),
    (
        "diff",
        "-DATABASE_URL=postgres://appuser:" + _DSN_PASSWORD + "@db.example.com:5432/prod",
        _DSN_PASSWORD,
    ),
    (
        "user message",
        "user says my api_" + "key is " + _CLOUD_KEY + " please help",
        _CLOUD_KEY,
    ),
)


class _Recorder:
    """Record every request sent through ``MockTransport`` (count and raw bytes)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []

    @property
    def count(self) -> int:
        return len(self.requests)


def _make_client(recorder: _Recorder) -> httpx.AsyncClient:
    """Build an httpx client that records every request (no real network)."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        recorder.bodies.append(request.content)
        return httpx.Response(200, json=_COMPLETION_RESPONSE)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


_COMPLETION_RESPONSE: dict[str, Any] = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": "test-model",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
}


def _use_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch to live mode with an OpenAI-compatible endpoint."""
    monkeypatch.setenv("BIANQUE_LLM_MODE", "live")
    monkeypatch.setenv("BIANQUE_LLM_BASE_URL", "http://llm.example.com:8000/v1")
    monkeypatch.setenv("BIANQUE_LLM_MODEL", "test-model")
    monkeypatch.delenv("BIANQUE_LLM_API_KEY", raising=False)


def _run[T](main: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run a coroutine in its own event loop (no pytest-asyncio dependency)."""
    return asyncio.run(main())


def _sample_prompt() -> RedactedPrompt:
    return RedactedPrompt(
        [
            RedactedMessage("system", redact_for_llm("you are an SRE assistant")),
            *(
                RedactedMessage("user", redact_for_llm(f"{entry}: {raw}"))
                for entry, raw, _ in _ENTRY_SAMPLES
            ),
        ]
    )


def _forged_redacted_text() -> RedactedText:
    """Bypass redact_for_llm() and forge a RedactedText that still holds a secret."""
    return RedactedText("pass" + "word=" + _PASSWORD, sentinel=_CONSTRUCTION_SENTINEL)


#: Context of every call in this module; the gates do not depend on it.
_CONTEXT = CallContext(agent="investigation", operation="plan")


def _gateway(recorder: _Recorder) -> LLMGateway:
    return build_gateway(get_settings(), http_client=_make_client(recorder))


# The request body carries none of the secrets from the input samples.
def test_outbound_body_has_no_plaintext_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_live(monkeypatch)
    recorder = _Recorder()

    async def main() -> None:
        gateway = _gateway(recorder)
        await gateway.complete(_sample_prompt(), context=_CONTEXT)

    _run(main)

    assert recorder.count >= 1, "no request was sent; the body assertion has nothing to check"
    blob = b"".join(recorder.bodies)
    for entry, _raw, secret in _ENTRY_SAMPLES:
        assert secret.encode("utf-8") not in blob, f"the {entry} secret appears in the body"
    # The body does carry the redacted content; otherwise "no plaintext" could just mean
    # no content was sent at all.
    assert b"redacted" in blob or b"high-entropy" in blob


# Runtime gate (unredacted input) and pre-send recheck (forged RedactedText).
def test_plain_str_raises_type_error_at_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_live(monkeypatch)
    recorder = _Recorder()

    # A prompt whose message content is a plain str, built the way deserialization
    # would build it: past RedactedPrompt.__init__ and its element check.
    smuggled = object.__new__(RedactedPrompt)
    object.__setattr__(
        smuggled,
        "messages",
        (RedactedMessage("user", "plain str"),),  # type: ignore[arg-type]
    )

    async def main() -> None:
        gateway = _gateway(recorder)
        with pytest.raises(TypeError):
            await gateway.complete("plain str", context=_CONTEXT)  # type: ignore[arg-type]
        # A bare sequence of messages is not a RedactedPrompt either.
        with pytest.raises(TypeError):
            await gateway.complete(
                [RedactedMessage("user", redact_for_llm("ping"))],  # type: ignore[arg-type]
                context=_CONTEXT,
            )
        with pytest.raises(TypeError):
            await gateway.complete(smuggled, context=_CONTEXT)

    _run(main)
    assert recorder.count == 0, "runtime gate breached: unredacted input was sent"


def test_forged_redacted_text_blocked_by_detect_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_live(monkeypatch)
    recorder = _Recorder()
    forged = _forged_redacted_text()

    async def main() -> None:
        gateway = _gateway(recorder)
        with pytest.raises(RedactionResidueError):
            await gateway.complete(
                RedactedPrompt([RedactedMessage("user", forged)]), context=_CONTEXT
            )

    _run(main)
    assert recorder.count == 0, "recheck breached: a forged RedactedText was sent"


# Tool call arguments are sent back to the model, so they pass the same gates.

#: Arguments of a tool call the model made, carrying three secrets of known shapes.
_TOOL_ARGUMENT_SECRETS = (_GITHUB_TOKEN, _CLOUD_KEY, _DSN_PASSWORD)


def _tool_call_arguments() -> str:
    return json.dumps(
        {
            "query": "tok" + "en=" + _GITHUB_TOKEN,
            "note": "the cloud key " + _CLOUD_KEY + " was rotated",
            "dsn": "postgres://appuser:" + _DSN_PASSWORD + "@db.example.com:5432/prod",
        }
    )


def _prompt_with_tool_turn() -> RedactedPrompt:
    """A user question, the model's tool call with secrets, and a tool result with one."""
    return RedactedPrompt(
        [
            RedactedMessage("user", redact_for_llm("why did checkout fail")),
            assistant_message("", [ToolCall("call-logs-1", "query_logs", _tool_call_arguments())]),
            tool_result_message(
                "call-logs-1", "log line: pass" + "word: '" + _PASSWORD + "' accepted"
            ),
        ]
    )


def test_outbound_body_has_no_secret_from_tool_call_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_live(monkeypatch)
    recorder = _Recorder()
    prompt = _prompt_with_tool_turn()

    async def main() -> None:
        await _gateway(recorder).complete(prompt, context=_CONTEXT)

    _run(main)

    assert recorder.count == 1
    blob = recorder.bodies[0]
    for secret in (*_TOOL_ARGUMENT_SECRETS, _PASSWORD):
        assert secret.encode("utf-8") not in blob, "a tool call secret appears in the body"
    # The tool turn itself was sent: the assistant call and the tool result that answers
    # it. Otherwise "no secret" could just mean the tool turn was dropped.
    body = json.loads(blob)
    assistant, tool = body["messages"][1], body["messages"][2]
    assert assistant["tool_calls"][0]["id"] == "call-logs-1"
    assert assistant["tool_calls"][0]["function"]["name"] == "query_logs"
    assert "<redacted:" in assistant["tool_calls"][0]["function"]["arguments"]
    assert tool["role"] == "tool"
    assert tool["tool_call_id"] == "call-logs-1"


def test_tool_call_arguments_not_redacted_raise_type_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_live(monkeypatch)
    recorder = _Recorder()

    # Tool call arguments that are a plain str, built past RedactedPrompt.__init__ the way
    # deserialization would build them.
    call = object.__new__(RedactedToolCall)
    object.__setattr__(call, "id", "call-1")
    object.__setattr__(call, "name", "query_logs")
    object.__setattr__(call, "arguments", _tool_call_arguments())
    message = RedactedMessage("assistant", redact_for_llm(""), tool_calls=(call,))
    smuggled = object.__new__(RedactedPrompt)
    object.__setattr__(smuggled, "messages", (message,))

    async def main() -> None:
        with pytest.raises(TypeError, match="tool call"):
            await _gateway(recorder).complete(smuggled, context=_CONTEXT)

    _run(main)
    assert recorder.count == 0, "runtime gate breached: unredacted tool call arguments were sent"


def test_forged_tool_call_arguments_blocked_by_detect_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_live(monkeypatch)
    recorder = _Recorder()
    forged_call = RedactedToolCall("call-1", "query_logs", _forged_redacted_text())
    prompt = RedactedPrompt(
        [
            RedactedMessage("user", redact_for_llm("why did checkout fail")),
            RedactedMessage("assistant", redact_for_llm(""), tool_calls=(forged_call,)),
        ]
    )

    async def main() -> None:
        with pytest.raises(RedactionResidueError):
            await _gateway(recorder).complete(prompt, context=_CONTEXT)

    _run(main)
    assert recorder.count == 0, "recheck breached: forged tool call arguments were sent"


def test_json_password_pair_in_tool_call_arguments_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_live(monkeypatch)
    recorder = _Recorder()
    arguments = json.dumps({"user": "appuser", "pass" + "word": _PASSWORD})
    prompt = RedactedPrompt(
        [
            RedactedMessage("user", redact_for_llm("log in to the database")),
            assistant_message("", [ToolCall("call-1", "connect", arguments)]),
        ]
    )

    async def main() -> None:
        await _gateway(recorder).complete(prompt, context=_CONTEXT)

    _run(main)
    assert recorder.count == 1
    assert _PASSWORD.encode("utf-8") not in recorder.bodies[0]


# The only constructor: without the sentinel there is no RedactedText.
def test_redacted_text_cannot_be_constructed_without_factory() -> None:
    with pytest.raises(TypeError):
        RedactedText("pass" + "word=" + _PASSWORD)


# Public surface regression: no public attribute of the gateway reaches an object that
# can send requests.

#: Top-level names of third-party modules that can send requests. Matching on the top
#: level of ``type(obj).__module__`` recognizes their instances without importing them.
_NETWORK_MODULE_ROOTS = frozenset({"openai", "httpx", "http", "socket", "urllib"})


def _is_network_capable(obj: object) -> bool:
    """Whether ``obj`` can send requests: a network client, or a provider-shaped object."""
    if type(obj).__module__.split(".")[0] in _NETWORK_MODULE_ROOTS:
        return True
    return callable(getattr(obj, "complete", None))


def _reachable_network_objects(root: object, *, max_depth: int = 3) -> list[str]:
    """Search breadth-first along public attributes and return paths to network objects.

    Depth 3 covers ``<root>.provider.client`` and its variants. Private names (prefixed
    with ``_``), functions, methods, classes, modules and scalars are not searched.
    """
    hits: list[str] = []
    seen: set[int] = {id(root)}
    frontier: list[tuple[str, object, int]] = [("<root>", root, 0)]
    while frontier:
        path, node, depth = frontier.pop()
        for name in dir(node):
            if name.startswith("_"):
                continue
            try:
                child: object = getattr(node, name)
            except Exception:  # an attribute that raises yields no usable object
                continue
            if child is None or isinstance(child, str | bytes | int | float | bool):
                continue
            if inspect.isroutine(child) or isinstance(child, type) or inspect.ismodule(child):
                continue
            if id(child) in seen:
                continue
            seen.add(id(child))
            child_path = f"{path}.{name}"
            if _is_network_capable(child):
                hits.append(child_path)
                continue
            if depth + 1 < max_depth:
                frontier.append((child_path, child, depth + 1))
    return hits


class _LeakyProviderStandIn:
    """Negative control: a provider that exposes its HTTP client publicly."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client


class _LeakyGatewayStandIn:
    """Negative control: a gateway that exposes its provider publicly."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.provider = _LeakyProviderStandIn(client)


def test_gateway_public_surface_exposes_no_network_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_live(monkeypatch)
    recorder = _Recorder()

    # Negative control first: the search does catch ``<gateway>.provider.client``. If
    # the control found nothing, a clean result for the real gateway would only show
    # that the search is not working.
    leaked = _reachable_network_objects(_LeakyGatewayStandIn(_make_client(_Recorder())))
    assert leaked == ["<root>.provider.client"], f"unexpected negative control hits: {leaked}"

    gateway = _gateway(recorder)
    hits = _reachable_network_objects(gateway)
    assert hits == [], f"the gateway's public surface reaches network objects: {hits}"

    # Observability is served by read-only scalars, without handing out objects.
    assert gateway.provider_name == "openai-compat"
    assert gateway.endpoint_host == "llm.example.com"
    assert recorder.count == 0, "this test reads only the public surface; nothing may be sent"
