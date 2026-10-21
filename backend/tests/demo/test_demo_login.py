"""``demo-login.sh`` writes a new random demo password, an MCP token, an A2A token and
their hashes on every run.

The hash command is ``bianque auth hash-password`` run by this interpreter, the same
command ``make replay`` runs inside the backend image.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

from bianque.auth.passwords import verify_password

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "demo" / "scripts" / "demo-login.sh"
HASH_COMMAND = [sys.executable, "-m", "bianque.cli", "auth", "hash-password"]
ENV_LINE = re.compile(
    r"BIANQUE_DEMO_USERS='(?P<users>[^']*)'\n"
    r"BIANQUE_SESSION_SECRET=(?P<secret>[0-9a-f]{64})\n"
    r"BIANQUE_MCP_CLIENTS='(?P<clients>[^']*)'\n"
    r"BIANQUE_A2A_CLIENTS='(?P<a2a_clients>[^']*)'\n"
)
FILES = ["a2a-token", "login.env", "mcp-token", "password"]


def _run(tmp_path: Path, *command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(SCRIPT),
            str(tmp_path / "login.env"),
            str(tmp_path / "password"),
            str(tmp_path / "mcp-token"),
            str(tmp_path / "a2a-token"),
            *command,
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "DEMO_DIR": str(tmp_path)},
        check=False,
    )


def _read(
    tmp_path: Path,
) -> tuple[str, dict[str, str], str, str, list[dict[str, object]], str, list[dict[str, object]]]:
    password = (tmp_path / "password").read_text().rstrip("\n")
    token = (tmp_path / "mcp-token").read_text().rstrip("\n")
    a2a_token = (tmp_path / "a2a-token").read_text().rstrip("\n")
    match = ENV_LINE.fullmatch((tmp_path / "login.env").read_text())
    assert match, (tmp_path / "login.env").read_text()
    return (
        password,
        json.loads(match["users"]),
        match["secret"],
        token,
        json.loads(match["clients"]),
        a2a_token,
        json.loads(match["a2a_clients"]),
    )


def test_writes_a_password_and_its_hash(tmp_path: Path) -> None:
    result = _run(tmp_path, *HASH_COMMAND)
    assert result.returncode == 0, result.stderr
    password, users, secret, token, clients, a2a_token, a2a_clients = _read(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{32}", password)
    assert list(users) == ["demo"] and verify_password(password, users["demo"])
    assert re.fullmatch(r"[0-9a-f]{64}", token)
    assert clients == [
        {
            "name": "claude-code",
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "tools": ["*"],
            "namespaces": ["demo-s1"],
        }
    ]
    assert re.fullmatch(r"[0-9a-f]{64}", a2a_token) and a2a_token != token
    assert a2a_clients == [
        {
            "name": "demo-agent",
            "token_sha256": hashlib.sha256(a2a_token.encode()).hexdigest(),
            "namespaces": ["demo-s1"],
        }
    ]
    for value in (password, secret, token, a2a_token):
        assert value not in result.stdout
    for name in FILES:
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600


def test_every_run_is_new(tmp_path: Path) -> None:
    assert _run(tmp_path, *HASH_COMMAND).returncode == 0
    first = _read(tmp_path)
    assert _run(tmp_path, *HASH_COMMAND).returncode == 0
    second = _read(tmp_path)
    assert first[0] != second[0] and first[2] != second[2] and first[3] != second[3]
    assert first[5] != second[5]


def test_failed_hash_keeps_the_earlier_files(tmp_path: Path) -> None:
    assert _run(tmp_path, *HASH_COMMAND).returncode == 0
    before = _read(tmp_path)
    for command in (["false"], ["echo", "not-a-hash"]):
        result = _run(tmp_path, *command)
        assert result.returncode != 0 and result.stderr.startswith("error: ")
        assert _read(tmp_path) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == FILES


def test_the_end_to_end_clients_are_two_new_ones(tmp_path: Path) -> None:
    script = REPO_ROOT / "demo" / "scripts" / "e2e-a2a.sh"
    files = [tmp_path / "e2e.env", tmp_path / "token-a", tmp_path / "token-b"]
    result = subprocess.run(
        [str(script), *map(str, files)],
        capture_output=True,
        text=True,
        env={**os.environ, "DEMO_DIR": str(tmp_path)},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    match = re.fullmatch(r"BIANQUE_A2A_CLIENTS='(?P<clients>[^']*)'\n", files[0].read_text())
    assert match, files[0].read_text()
    token_a, token_b = (path.read_text().rstrip("\n") for path in files[1:])
    assert json.loads(match["clients"]) == [
        {
            "name": "e2e-agent-a",
            "token_sha256": hashlib.sha256(token_a.encode()).hexdigest(),
            "namespaces": ["demo-s1"],
        },
        {
            "name": "e2e-agent-b",
            "token_sha256": hashlib.sha256(token_b.encode()).hexdigest(),
            "namespaces": ["e2e-elsewhere"],
        },
    ]
    assert token_a != token_b and token_a not in result.stdout
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["e2e.env", "token-a", "token-b"]
