"""Settings: the ``BIANQUE_`` prefix convention, mode defaults and field mapping."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

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


def test_default_modes_need_no_credential_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_bianque_env(monkeypatch)
    settings = get_settings()
    assert settings.tool_backend == "pack"
    assert settings.llm_mode == "scripted"
    assert settings.embedding_mode == "fixture"
    assert settings.llm_api_key is None

    # The default set builds a working gateway without any key and without an endpoint.
    gateway = build_gateway(settings)
    assert gateway.endpoint_host is None


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
