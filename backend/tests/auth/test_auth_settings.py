"""Demo account and session settings: parsed from the environment, checked at startup."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from pydantic_settings.exceptions import SettingsError

from bianque.auth.passwords import hash_password
from bianque.config import get_settings

HASH = hash_password("pw", rounds=4)
SECRET = "s" * 32


def _users(users: dict[str, str]) -> str:
    return json.dumps(users)


def test_defaults_have_no_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "BIANQUE_DEMO_USERS",
        "BIANQUE_SESSION_SECRET",
        "BIANQUE_SESSION_TTL_SECONDS",
        "BIANQUE_SESSION_COOKIE_SECURE",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = get_settings()
    assert settings.demo_users == {}
    assert settings.session_secret is None
    assert settings.session_ttl_seconds == 43200
    assert settings.session_cookie_secure is True


def test_accounts_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIANQUE_DEMO_USERS", _users({"demo": HASH}))
    monkeypatch.setenv("BIANQUE_SESSION_SECRET", SECRET)
    monkeypatch.setenv("BIANQUE_SESSION_TTL_SECONDS", "600")
    monkeypatch.setenv("BIANQUE_SESSION_COOKIE_SECURE", "false")
    settings = get_settings()
    assert {name: value.get_secret_value() for name, value in settings.demo_users.items()} == {
        "demo": HASH
    }
    assert settings.session_secret is not None
    assert settings.session_secret.get_secret_value() == SECRET
    assert settings.session_ttl_seconds == 600
    assert settings.session_cookie_secure is False
    # Neither the hashes nor the secret appear in any representation.
    for text in (repr(settings), str(settings), repr(settings.demo_users)):
        assert HASH not in text and SECRET not in text


def test_empty_values_mean_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIANQUE_DEMO_USERS", "")
    monkeypatch.setenv("BIANQUE_SESSION_SECRET", "")
    settings = get_settings()
    assert settings.demo_users == {} and settings.session_secret is None


@pytest.mark.parametrize(
    ("users", "secret"),
    [
        ('["demo"]', SECRET),
        ("not json", SECRET),
        (_users({"demo": "plaintext-password"}), SECRET),
        (_users({"demo": HASH[:-1]}), SECRET),
        (_users({"Demo User": HASH}), SECRET),
        (_users({"": HASH}), SECRET),
        (_users({"demo": HASH}), None),
        (_users({"demo": HASH}), "short"),
    ],
    ids=[
        "not-an-object",
        "not-json",
        "plaintext",
        "truncated-hash",
        "bad-username",
        "empty-username",
        "no-secret",
        "short-secret",
    ],
)
def test_bad_account_settings_fail(
    monkeypatch: pytest.MonkeyPatch, users: str, secret: str | None
) -> None:
    monkeypatch.setenv("BIANQUE_DEMO_USERS", users)
    if secret is None:
        monkeypatch.delenv("BIANQUE_SESSION_SECRET", raising=False)
    else:
        monkeypatch.setenv("BIANQUE_SESSION_SECRET", secret)
    with pytest.raises((ValidationError, SettingsError)):
        get_settings()


def test_session_ttl_has_a_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIANQUE_SESSION_TTL_SECONDS", "59")
    with pytest.raises(ValidationError):
        get_settings()
