"""``recorded`` mode: write recordings in ``live`` mode, replay them by request key.

- A ``live`` call with ``BIANQUE_LLM_RECORD_DIR`` set writes one recording file; a
  ``recorded`` gateway over that directory returns the same completion with no network.
- A request with no recording raises ``RecordingNotFoundError`` naming the request key,
  the agent and the call site; there is no fallback to another mode.
- A recording with another schema version, a broken file, or two files with the same
  key raise ``RecordingFormatError`` naming the file.
- A case reads only its own directory of recordings.
- The recordings directory is read on the first call, not when the gateway is built.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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

from bianque.agents.prompting import prompt_hashes
from bianque.agents.startup import load_skill_catalog
from bianque.config.manifest import ManifestError, RecordingManifest, write_manifest
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


def _manifest(**changes: Any) -> RecordingManifest:
    fields: dict[str, Any] = {
        "schema_version": 2,
        "model": MODEL,
        "extra_body": json.loads(_EXTRA_BODY),
        "endpoint_host": "llm.example.test",
        "recorded_at": "2026-10-20T12:00:00+00:00",
        "cases": ["s1-a", "s1-d"],
        "prompts": {"plan.md": "0" * 64},
        "skills": {"investigate-pod-restarts/SKILL.md": "2" * 64},
        "tool_schemas_sha256": "1" * 64,
    }
    return RecordingManifest.model_validate({**fields, **changes})


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
    case = tmp_path / "recordings" / "case-oom"
    recorded = await _record_one(monkeypatch, case)

    endpoint = FakeEndpoint()
    gateway = _replay_gateway(monkeypatch, case, endpoint)
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
    await _record_one(monkeypatch, tmp_path)
    [original] = list(tmp_path.iterdir())
    copy = tmp_path / "copy.json"
    copy.write_text(original.read_text(encoding="utf-8"), encoding="utf-8")

    gateway = _replay_gateway(monkeypatch, tmp_path)
    with pytest.raises(RecordingFormatError) as excinfo:
        await _ask(gateway)

    message = str(excinfo.value)
    assert _expected_key() in message
    assert str(original) in message
    assert str(copy) in message


async def test_a_case_reads_only_its_own_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two cases record the same first request; each replays its own recording."""
    await _record_one(monkeypatch, tmp_path / "s1-a")
    await _record_one(monkeypatch, tmp_path / "s1-d")
    write_manifest(tmp_path, _manifest())

    # The model and the extra body come from the manifest.
    settings = settings_from_env(
        monkeypatch, {"BIANQUE_LLM_MODE": "recorded", "BIANQUE_LLM_RECORDINGS_DIR": tmp_path}
    )
    assert settings.llm_model == MODEL
    replayed = await _ask(build_gateway(settings.for_case("s1-d"), sleep=SleepLog()))
    assert replayed.text == "checking logs"

    # The root holds case directories and the manifest, never a recording.
    with pytest.raises(RecordingNotFoundError, match="one without a case has no recordings"):
        await _ask(build_gateway(settings.for_case(None), sleep=SleepLog()))


async def test_recordings_are_read_on_first_call_not_at_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

    # Building the gateway reads nothing, so the broken file does not fail it.
    gateway = _replay_gateway(monkeypatch, tmp_path)

    with pytest.raises(RecordingFormatError, match="broken.json"):
        await _ask(gateway)


# -- The recording manifest ------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"BIANQUE_LLM_MODEL": "another-model"}, "unset it to replay"),
        ({"BIANQUE_LLM_EXTRA_BODY": "{}"}, None),
        ({"BIANQUE_LLM_EXTRA_BODY": '{"thinking": {"type": "enabled"}}'}, "unset it to replay"),
        ({"BIANQUE_LLM_MODEL": MODEL, "BIANQUE_LLM_EXTRA_BODY": _EXTRA_BODY}, None),
    ],
)
def test_configured_values_must_agree_with_the_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: dict[str, str], message: str | None
) -> None:
    write_manifest(tmp_path, _manifest())
    full: dict[str, str | Path] = {
        "BIANQUE_LLM_MODE": "recorded",
        "BIANQUE_LLM_RECORDINGS_DIR": tmp_path,
        **env,
    }
    if message is None:
        settings = settings_from_env(monkeypatch, full)
        assert (settings.llm_model, settings.llm_extra_body) == (MODEL, json.loads(_EXTRA_BODY))
    else:
        with pytest.raises(ManifestError, match=message):
            settings_from_env(monkeypatch, full)


def test_other_modes_ignore_the_manifest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    write_manifest(tmp_path, _manifest())
    settings = settings_from_env(
        monkeypatch, {"BIANQUE_LLM_MODE": "scripted", "BIANQUE_LLM_RECORDINGS_DIR": tmp_path}
    )
    assert settings.llm_model == ""


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('{"schema_version": 1}', "schema_version is 1, expected 2"),
        ("not json", "cannot read the recording manifest"),
        ('{"schema_version": 2, "model": "m"}', "manifest.json"),
    ],
)
def test_a_malformed_manifest_fails_naming_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str, message: str
) -> None:
    (tmp_path / "manifest.json").write_text(content, encoding="utf-8")
    with pytest.raises(ManifestError, match=message):
        settings_from_env(
            monkeypatch, {"BIANQUE_LLM_MODE": "recorded", "BIANQUE_LLM_RECORDINGS_DIR": tmp_path}
        )


# -- Prompt files and skills against the manifest ---------------------------------------


def _skills(root: Path, body: str = "Read the pod status.\n") -> Path:
    skill = root / "skills" / "check-pods"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: check-pods\ndescription: Check pods.\n---\n{body}", encoding="utf-8"
    )
    return root / "skills"


def _recorded_with(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, skills: Path) -> Any:
    return settings_from_env(
        monkeypatch,
        {
            "BIANQUE_LLM_MODE": "recorded",
            "BIANQUE_LLM_RECORDINGS_DIR": tmp_path / "rec",
            "BIANQUE_SKILLS_DIR": skills,
        },
    )


def _write_current(tmp_path: Path, skills_dir: Path, **changes: Any) -> None:
    from bianque.skills import load_skills

    (tmp_path / "rec").mkdir(exist_ok=True)
    values: dict[str, Any] = {
        "prompts": prompt_hashes(),
        "skills": load_skills(skills_dir).hashes(),
        **changes,
    }
    write_manifest(tmp_path / "rec", _manifest(**values))


def test_recorded_mode_starts_when_prompts_and_skills_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    skills = _skills(tmp_path)
    _write_current(tmp_path, skills)
    catalog = load_skill_catalog(_recorded_with(monkeypatch, tmp_path, skills))
    assert catalog.names == ("check-pods",)


def test_a_changed_skill_fails_recorded_mode_naming_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    skills = _skills(tmp_path)
    _write_current(tmp_path, skills)
    (skills / "check-pods" / "SKILL.md").write_text(
        "---\nname: check-pods\ndescription: Check pods.\n---\nRead the events.\n"
    )
    with pytest.raises(ManifestError, match=r"check-pods/SKILL\.md.*record again"):
        load_skill_catalog(_recorded_with(monkeypatch, tmp_path, skills))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"skills": {}}, r"check-pods/SKILL\.md is not in the recording manifest"),
        (
            {"skills": {"check-pods/SKILL.md": "3" * 64, "gone/SKILL.md": "4" * 64}},
            r"check-pods/SKILL\.md differs .*, 1 more",
        ),
        ({"prompts": {**prompt_hashes(), "plan.md": "5" * 64}}, r"prompts/plan\.md"),
        ({"prompts": {"plan.md": prompt_hashes()["plan.md"]}}, r"prompts/assess\.md"),
    ],
)
def test_every_difference_from_the_manifest_fails_recorded_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, changes: dict[str, Any], message: str
) -> None:
    skills = _skills(tmp_path)
    _write_current(tmp_path, skills, **changes)
    with pytest.raises(ManifestError, match=message):
        load_skill_catalog(_recorded_with(monkeypatch, tmp_path, skills))


def test_a_recorded_skill_missing_on_disk_fails_recorded_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from bianque.skills import load_skills

    skills = _skills(tmp_path)
    recorded = {**load_skills(skills).hashes(), "gone/SKILL.md": "4" * 64}
    _write_current(tmp_path, skills, skills=recorded)
    with pytest.raises(ManifestError, match=r"gone/SKILL\.md is in the recording manifest"):
        load_skill_catalog(_recorded_with(monkeypatch, tmp_path, skills))


def test_other_modes_do_not_compare_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    skills = _skills(tmp_path)
    _write_current(tmp_path, skills, prompts={})
    settings = settings_from_env(
        monkeypatch,
        {
            "BIANQUE_LLM_MODE": "scripted",
            "BIANQUE_LLM_RECORDINGS_DIR": tmp_path / "rec",
            "BIANQUE_SKILLS_DIR": skills,
        },
    )
    assert load_skill_catalog(settings).names == ("check-pods",)
