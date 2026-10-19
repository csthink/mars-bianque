"""``recorded`` mode: write recordings in ``live`` mode, replay them by request key.

- A ``live`` call with ``BIANQUE_LLM_RECORD_DIR`` set writes one recording file; a
  ``recorded`` gateway over that directory returns the same completion with no network.
- A request with no recording raises ``RecordingNotFoundError`` naming the request key,
  the agent and the call site; there is no fallback to another mode.
- A recording with another schema version, a broken file, or two files with the same
  key raise ``RecordingFormatError`` naming the file.
- The recordings directory is read on the first call, not when the gateway is built.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from llm_fakes import (
    CONTEXT,
    MODEL,
    FakeEndpoint,
    SleepLog,
    live_env,
    ok,
    prompt,
    settings_from_env,
    status,
)

from bianque.llm import (
    CallContext,
    LLMCompletion,
    LLMGateway,
    LLMUnavailableError,
    LLMUsage,
    MemoryCallRecorder,
    RecordingFormatError,
    RecordingNotFoundError,
    ToolCall,
    ToolSpec,
    build_gateway,
)
from bianque.llm.request import ModelRequest, request_key

_EXTRA_BODY = '{"thinking": {"type": "disabled"}}'

_QUERY_LOGS = ToolSpec(
    name="query_logs",
    description="Search the logs of a pod.",
    parameters={
        "type": "object",
        "properties": {"pod": {"type": "string"}},
        "required": ["pod"],
    },
)

_RECORDED_CALLS = (ToolCall("call-7f3a", "query_logs", '{"pod": "checkout-7"}'),)


async def _ask(gateway: LLMGateway, text: str = "why did checkout restart") -> LLMCompletion:
    return await gateway.complete(
        prompt(("system", "you are an SRE assistant"), ("user", text)),
        context=CONTEXT,
        tools=(_QUERY_LOGS,),
        tool_choice="auto",
    )


def _recording_gateway(
    monkeypatch: pytest.MonkeyPatch, record_dir: Path, endpoint: FakeEndpoint
) -> LLMGateway:
    settings = settings_from_env(
        monkeypatch,
        live_env(BIANQUE_LLM_RECORD_DIR=record_dir, BIANQUE_LLM_EXTRA_BODY=_EXTRA_BODY),
    )
    return build_gateway(settings, http_client=endpoint.client(), sleep=SleepLog())


def _replay_gateway(
    monkeypatch: pytest.MonkeyPatch,
    recordings_dir: Path,
    endpoint: FakeEndpoint | None = None,
    recorder: MemoryCallRecorder | None = None,
) -> LLMGateway:
    # The same model and extra body as when recording: both are part of the request key.
    settings = settings_from_env(
        monkeypatch,
        {
            "BIANQUE_LLM_MODE": "recorded",
            "BIANQUE_LLM_MODEL": MODEL,
            "BIANQUE_LLM_EXTRA_BODY": _EXTRA_BODY,
            "BIANQUE_LLM_RECORDINGS_DIR": recordings_dir,
        },
    )
    return build_gateway(
        settings,
        recorder=recorder,
        http_client=endpoint.client() if endpoint else None,
        sleep=SleepLog(),
    )


async def _record_one(monkeypatch: pytest.MonkeyPatch, record_dir: Path) -> LLMCompletion:
    endpoint = FakeEndpoint(ok(text="checking logs", tool_calls=_RECORDED_CALLS, usage=(321, 45)))
    completion = await _ask(_recording_gateway(monkeypatch, record_dir, endpoint))
    assert endpoint.count == 1
    return completion


def _expected_key(text: str = "why did checkout restart") -> str:
    """The request key of ``_ask(text)`` under the replay settings."""
    return request_key(
        ModelRequest(
            agent=CONTEXT.agent,
            operation=CONTEXT.operation,
            prompt=prompt(("system", "you are an SRE assistant"), ("user", text)),
            model=MODEL,
            tools=(_QUERY_LOGS,),
            tool_choice="auto",
            extra_body={"thinking": {"type": "disabled"}},
        )
    )


# -- Recording and replay ------------------------------------------------------------


async def test_live_call_writes_one_recording_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record_dir = tmp_path / "recordings" / "case-oom"
    await _record_one(monkeypatch, record_dir)

    key = _expected_key()
    files = sorted(record_dir.iterdir())
    assert [path.name for path in files] == [f"investigation-{key[:16]}.json"]
    document = json.loads(files[0].read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    assert document["key"] == key
    assert document["agent"] == "investigation"
    assert document["operation"] == "plan"
    assert document["model"] == MODEL
    assert document["recorded_at"]
    assert document["request"]["agent"] == "investigation"
    assert document["request"]["messages"][1] == {
        "role": "user",
        "content": "why did checkout restart",
    }
    assert document["response"] == {
        "text": "checking logs",
        "model": MODEL,
        "finish_reason": "tool_calls",
        "tool_calls": [
            {"id": "call-7f3a", "name": "query_logs", "arguments": '{"pod": "checkout-7"}'}
        ],
        "usage": {"prompt_tokens": 321, "completion_tokens": 45, "estimated": False},
    }


async def test_failed_live_call_writes_no_recording(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record_dir = tmp_path / "recordings"
    gateway = _recording_gateway(monkeypatch, record_dir, FakeEndpoint(status(503)))

    with pytest.raises(LLMUnavailableError):
        await _ask(gateway)

    assert not record_dir.exists() or not any(record_dir.iterdir())


async def test_recorded_mode_replays_the_recorded_completion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "recordings"
    # The file is written one level down; the replay indexes the root recursively.
    recorded = await _record_one(monkeypatch, root / "case-oom")

    endpoint = FakeEndpoint()
    gateway = _replay_gateway(monkeypatch, root, endpoint)
    assert gateway.provider_name == "recorded"
    assert gateway.endpoint_host is None

    replayed = await _ask(gateway)

    assert replayed == recorded
    assert replayed.text == "checking logs"
    assert replayed.tool_calls == _RECORDED_CALLS
    # Usage is the one measured when recording, not an estimate.
    assert replayed.usage == LLMUsage(321, 45, estimated=False)
    assert endpoint.count == 0, "recorded mode sent a request"


async def test_replay_does_not_depend_on_run_specific_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded = await _record_one(monkeypatch, tmp_path)
    gateway = _replay_gateway(monkeypatch, tmp_path)

    replayed = await gateway.complete(
        prompt(("system", "you are an SRE assistant"), ("user", "why did checkout restart")),
        context=CallContext(
            agent="investigation", operation="plan", investigation_id="inv-9999", session_id=99
        ),
        tools=(_QUERY_LOGS,),
    )

    assert replayed == recorded


# -- Lookup failures -----------------------------------------------------------------


async def test_unknown_request_raises_not_found_with_key_and_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    await _record_one(monkeypatch, tmp_path)
    recorder = MemoryCallRecorder()
    gateway = _replay_gateway(monkeypatch, tmp_path, recorder=recorder)

    with pytest.raises(RecordingNotFoundError) as excinfo:
        await _ask(gateway, "why did payments restart")

    message = str(excinfo.value)
    assert _expected_key("why did payments restart") in message
    assert "'investigation'" in message
    assert "'plan'" in message
    assert "live" in message
    # The failure is not retried and is recorded like any other failed call.
    [event] = recorder.events
    assert event.status == "error"
    assert event.retries == 0
    assert event.error is not None and "RecordingNotFoundError" in event.error


async def test_missing_recordings_directory_raises_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gateway = _replay_gateway(monkeypatch, tmp_path / "does-not-exist")

    with pytest.raises(RecordingNotFoundError):
        await _ask(gateway)


async def test_schema_version_mismatch_raises_format_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    await _record_one(monkeypatch, tmp_path)
    [path] = list(tmp_path.iterdir())
    document = json.loads(path.read_text(encoding="utf-8"))
    document["schema_version"] = 2
    path.write_text(json.dumps(document), encoding="utf-8")

    gateway = _replay_gateway(monkeypatch, tmp_path)
    with pytest.raises(RecordingFormatError) as excinfo:
        await _ask(gateway)

    message = str(excinfo.value)
    assert str(path) in message
    assert "2" in message


async def test_recording_with_missing_field_raises_format_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    await _record_one(monkeypatch, tmp_path)
    [path] = list(tmp_path.iterdir())
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["response"]["usage"]
    path.write_text(json.dumps(document), encoding="utf-8")

    gateway = _replay_gateway(monkeypatch, tmp_path)
    with pytest.raises(RecordingFormatError, match=str(path)):
        await _ask(gateway)


async def test_duplicate_request_key_raises_format_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    await _record_one(monkeypatch, tmp_path / "case-a")
    [original] = list((tmp_path / "case-a").iterdir())
    copy = tmp_path / "case-b" / original.name
    copy.parent.mkdir()
    copy.write_text(original.read_text(encoding="utf-8"), encoding="utf-8")

    gateway = _replay_gateway(monkeypatch, tmp_path)
    with pytest.raises(RecordingFormatError) as excinfo:
        await _ask(gateway)

    message = str(excinfo.value)
    assert _expected_key() in message
    assert str(original) in message
    assert str(copy) in message


async def test_recordings_are_read_on_first_call_not_at_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

    # Building the gateway reads nothing, so the broken file does not fail it.
    gateway = _replay_gateway(monkeypatch, tmp_path)

    with pytest.raises(RecordingFormatError, match="broken.json"):
        await _ask(gateway)
