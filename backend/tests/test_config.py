"""Settings: the ``BIANQUE_`` prefix convention, mode defaults and field mapping."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings.exceptions import SettingsError

from bianque.config import ENV_PREFIX, Settings, get_settings
from bianque.llm.gateway import build_gateway


def _clear_bianque_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name)


def test_env_prefix_is_bianque() -> None:
    assert ENV_PREFIX == "BIANQUE_"
    assert Settings.model_config["env_prefix"] == "BIANQUE_"


def test_every_field_maps_to_a_prefixed_env_var() -> None:
    # An alias on a field would replace BIANQUE_<FIELD> with an arbitrary name.
    for name, field in Settings.model_fields.items():
        assert field.alias is None, f"field {name} declares alias {field.alias!r}"
        assert field.validation_alias is None, (
            f"field {name} declares validation_alias {field.validation_alias!r}"
        )


def test_default_modes_need_no_credential_or_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_bianque_env(monkeypatch)
    # Run from an empty directory: the default recordings directory does not exist there,
    # so building the gateway proves that construction reads no recording files.
    monkeypatch.chdir(tmp_path)
    settings = get_settings()
    assert settings.tool_backend == "pack"
    assert settings.llm_mode == "recorded"
    assert settings.embedding_mode == "fixture"
    assert settings.llm_api_key is None

    # The default set builds a working gateway without any key, without an endpoint
    # and without touching the recordings directory.
    gateway = build_gateway(settings)
    assert gateway.provider_name == "recorded"
    assert gateway.endpoint_host is None
    assert not (tmp_path / "eval").exists()


def test_model_gateway_setting_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_bianque_env(monkeypatch)
    settings = get_settings()
    assert settings.llm_max_retries == 2
    assert settings.llm_extra_body == {}
    assert settings.llm_recordings_dir == Path("eval/recordings")
    assert settings.llm_record_dir is None


def test_model_gateway_settings_read_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BIANQUE_LLM_MAX_RETRIES", "5")
    monkeypatch.setenv("BIANQUE_LLM_EXTRA_BODY", '{"thinking": {"type": "disabled"}}')
    monkeypatch.setenv("BIANQUE_LLM_RECORDINGS_DIR", str(tmp_path / "replay"))
    monkeypatch.setenv("BIANQUE_LLM_RECORD_DIR", str(tmp_path / "record"))
    settings = get_settings()
    assert settings.llm_max_retries == 5
    assert settings.llm_extra_body == {"thinking": {"type": "disabled"}}
    assert settings.llm_recordings_dir == tmp_path / "replay"
    assert settings.llm_record_dir == tmp_path / "record"


@pytest.mark.parametrize("raw", ["not json", "[1, 2]"])
def test_extra_body_must_be_a_json_object(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("BIANQUE_LLM_EXTRA_BODY", raw)
    with pytest.raises((ValidationError, SettingsError)):
        get_settings()


def test_empty_extra_body_env_means_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIANQUE_LLM_EXTRA_BODY", "")
    assert get_settings().llm_extra_body == {}


def test_env_var_read_with_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    # tool_backend is read from BIANQUE_TOOL_BACKEND: env_prefix maps the field name.
    monkeypatch.setenv("BIANQUE_TOOL_BACKEND", "live")
    assert get_settings().tool_backend == "live"


@pytest.mark.parametrize(
    "env_name", ["BIANQUE_TOOL_BACKEND", "BIANQUE_LLM_MODE", "BIANQUE_EMBEDDING_MODE"]
)
def test_unknown_mode_is_rejected(monkeypatch: pytest.MonkeyPatch, env_name: str) -> None:
    monkeypatch.setenv(env_name, "no-such-mode")
    with pytest.raises(ValidationError):
        get_settings()


def test_a_case_narrows_the_recordings_directories(tmp_path: Path) -> None:
    settings = Settings(llm_recordings_dir=tmp_path / "rec", llm_record_dir=tmp_path / "new")
    narrowed = settings.for_case("s1-b")
    assert narrowed.llm_recordings_dir == tmp_path / "rec" / "s1-b"
    assert narrowed.llm_record_dir == tmp_path / "new" / "s1-b"
    assert settings.for_case(None) is settings
    assert Settings(llm_recordings_dir=tmp_path).for_case("s1-a").llm_record_dir is None
