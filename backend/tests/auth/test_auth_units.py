"""Demo account passwords and session tokens, without a database or an application."""

from __future__ import annotations

import base64
import io
import json

import pytest

from bianque.auth.authenticator import Authenticator
from bianque.auth.passwords import BCRYPT_HASH, MAX_PASSWORD_BYTES, hash_password, verify_password
from bianque.auth.sessions import issue_token, verify_token
from bianque.cli import main

#: bcrypt's lowest cost keeps the tests fast; the cost is part of the hash, so
#: verification does not depend on it.
FAST = 4
SECRET = b"s" * 32
NOW = 1_800_000_000.0
TTL = 3600


def test_hash_verifies_and_has_the_bcrypt_format() -> None:
    hashed = hash_password("correct horse", rounds=FAST)
    assert BCRYPT_HASH.fullmatch(hashed)
    assert verify_password("correct horse", hashed)
    assert not verify_password("correct hors", hashed)


def test_default_cost_is_twelve() -> None:
    assert hash_password("x").startswith("$2b$12$")


def test_password_over_the_bcrypt_limit() -> None:
    hashed = hash_password("x" * MAX_PASSWORD_BYTES, rounds=FAST)
    # bcrypt reads at most 72 bytes; a longer password never matches instead of being
    # silently truncated to a prefix that would.
    assert not verify_password("x" * (MAX_PASSWORD_BYTES + 1), hashed)
    with pytest.raises(ValueError, match="72 bytes"):
        hash_password("x" * (MAX_PASSWORD_BYTES + 1), rounds=FAST)
    # The limit counts UTF-8 bytes, not characters.
    with pytest.raises(ValueError, match="72 bytes"):
        hash_password("界" * 25, rounds=FAST)


def test_malformed_hash_never_verifies() -> None:
    assert not verify_password("x", "not a hash")


@pytest.fixture
def users() -> dict[str, str]:
    return {"demo": hash_password("pw", rounds=FAST)}


def test_token_round_trip(users: dict[str, str]) -> None:
    token = issue_token("demo", users["demo"], secret=SECRET, now=NOW)
    assert verify_token(token, users, secret=SECRET, ttl_seconds=TTL, now=NOW + 10) == "demo"


def test_token_carries_only_the_user_and_the_issue_time(users: dict[str, str]) -> None:
    token = issue_token("demo", users["demo"], secret=SECRET, now=NOW)
    payload = token.split(".")[0]
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert decoded == {"u": "demo", "iat": int(NOW)}


def _tamper_payload(token: str, payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f"{encoded}.{token.split('.')[1]}"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t[:-2] + ("AA" if not t.endswith("AA") else "BB"),
        lambda t: _tamper_payload(t, {"u": "demo", "iat": int(NOW) + 5}),
        lambda t: t.replace(".", ""),
        lambda t: "",
        lambda t: "a.b.c",
        lambda t: "!!!.???",
    ],
    ids=["signature", "payload", "no-separator", "empty", "three-parts", "not-base64"],
)
def test_tampered_token_is_rejected(users: dict[str, str], mutate: object) -> None:
    token = issue_token("demo", users["demo"], secret=SECRET, now=NOW)
    bad = mutate(token)  # type: ignore[operator]
    assert verify_token(bad, users, secret=SECRET, ttl_seconds=TTL, now=NOW) is None


def test_expired_token_is_rejected(users: dict[str, str]) -> None:
    token = issue_token("demo", users["demo"], secret=SECRET, now=NOW)
    assert verify_token(token, users, secret=SECRET, ttl_seconds=TTL, now=NOW + TTL - 1)
    assert verify_token(token, users, secret=SECRET, ttl_seconds=TTL, now=NOW + TTL) is None


def test_token_from_the_future_is_rejected(users: dict[str, str]) -> None:
    token = issue_token("demo", users["demo"], secret=SECRET, now=NOW + 61)
    assert verify_token(token, users, secret=SECRET, ttl_seconds=TTL, now=NOW) is None
    # A small clock difference between processes is tolerated.
    token = issue_token("demo", users["demo"], secret=SECRET, now=NOW + 30)
    assert verify_token(token, users, secret=SECRET, ttl_seconds=TTL, now=NOW) == "demo"


def test_new_secret_invalidates_tokens(users: dict[str, str]) -> None:
    token = issue_token("demo", users["demo"], secret=SECRET, now=NOW)
    assert verify_token(token, users, secret=b"t" * 32, ttl_seconds=TTL, now=NOW) is None


def test_new_password_invalidates_tokens(users: dict[str, str]) -> None:
    token = issue_token("demo", users["demo"], secret=SECRET, now=NOW)
    changed = {"demo": hash_password("pw", rounds=FAST)}
    # Same password, new salt: the hash differs, so the signing key differs.
    assert verify_token(token, changed, secret=SECRET, ttl_seconds=TTL, now=NOW) is None


def test_removed_user_is_rejected(users: dict[str, str]) -> None:
    token = issue_token("demo", users["demo"], secret=SECRET, now=NOW)
    assert verify_token(token, {}, secret=SECRET, ttl_seconds=TTL, now=NOW) is None


def test_token_of_one_user_does_not_sign_another() -> None:
    users = {"demo": hash_password("pw", rounds=FAST), "ops": hash_password("pw", rounds=FAST)}
    token = issue_token("demo", users["demo"], secret=SECRET, now=NOW)
    forged = _tamper_payload(token, {"u": "ops", "iat": int(NOW)})
    assert verify_token(forged, users, secret=SECRET, ttl_seconds=TTL, now=NOW) is None


def test_authenticator_login_and_lookup(users: dict[str, str]) -> None:
    clock = [NOW]
    auth = Authenticator(users, secret=SECRET, ttl_seconds=TTL, clock=lambda: clock[0])
    assert auth.login("demo", "wrong") is None
    assert auth.login("nobody", "pw") is None
    token = auth.login("demo", "pw")
    assert token is not None and auth.user_of(token) == "demo"
    clock[0] = NOW + TTL
    assert auth.user_of(token) is None


def test_authenticator_without_accounts_admits_nobody() -> None:
    auth = Authenticator({}, secret=None, ttl_seconds=TTL)
    assert auth.login("demo", "pw") is None
    assert auth.user_of("anything") is None


def test_authenticator_with_accounts_needs_a_secret(users: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="BIANQUE_SESSION_SECRET"):
        Authenticator(users, secret=None, ttl_seconds=TTL)


def test_hash_password_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("s3cret-pass\n"))
    assert main(["auth", "hash-password"]) == 0
    printed = capsys.readouterr().out.strip()
    assert BCRYPT_HASH.fullmatch(printed) and verify_password("s3cret-pass", printed)


@pytest.mark.parametrize("stdin", ["", "\n", "x" * 73 + "\n"], ids=["eof", "empty", "too-long"])
def test_hash_password_command_rejects(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stdin: str
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert main(["auth", "hash-password"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("error: ")
