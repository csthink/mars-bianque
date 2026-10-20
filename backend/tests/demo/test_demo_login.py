"""``demo-login.sh`` writes a new random demo password and a matching hash on every run.

The hash command is ``bianque auth hash-password`` run by this interpreter, the same
command ``make replay`` runs inside the backend image.
"""

from __future__ import annotations

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
)


def _run(tmp_path: Path, *command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), str(tmp_path / "login.env"), str(tmp_path / "password"), *command],
        capture_output=True,
        text=True,
        env={**os.environ, "DEMO_DIR": str(tmp_path)},
        check=False,
    )


def _read(tmp_path: Path) -> tuple[str, dict[str, str], str]:
    password = (tmp_path / "password").read_text().rstrip("\n")
    match = ENV_LINE.fullmatch((tmp_path / "login.env").read_text())
    assert match, (tmp_path / "login.env").read_text()
    return password, json.loads(match["users"]), match["secret"]


def test_writes_a_password_and_its_hash(tmp_path: Path) -> None:
    result = _run(tmp_path, *HASH_COMMAND)
    assert result.returncode == 0, result.stderr
    password, users, secret = _read(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{32}", password)
    assert list(users) == ["demo"] and verify_password(password, users["demo"])
    assert password not in result.stdout and secret not in result.stdout
    for name in ("password", "login.env"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600


def test_every_run_is_new(tmp_path: Path) -> None:
    assert _run(tmp_path, *HASH_COMMAND).returncode == 0
    first = _read(tmp_path)
    assert _run(tmp_path, *HASH_COMMAND).returncode == 0
    second = _read(tmp_path)
    assert first[0] != second[0] and first[2] != second[2]


def test_failed_hash_keeps_the_earlier_files(tmp_path: Path) -> None:
    assert _run(tmp_path, *HASH_COMMAND).returncode == 0
    before = _read(tmp_path)
    for command in (["false"], ["echo", "not-a-hash"]):
        result = _run(tmp_path, *command)
        assert result.returncode != 0 and result.stderr.startswith("error: ")
        assert _read(tmp_path) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["login.env", "password"]
