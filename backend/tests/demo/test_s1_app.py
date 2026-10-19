"""The S1 demo application fails exactly the way the fault cases rely on.

``demo/s1-app/app.py`` runs as a subprocess, the same way the container runs it.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[3] / "demo" / "s1-app" / "app.py"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _run_app(env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-u", str(APP)],
        env={"PATH": os.environ.get("PATH", ""), **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _get(port: int, path: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


@pytest.fixture
def running_app() -> Iterator[tuple[int, subprocess.Popen[str]]]:
    port = _free_port()
    proc = _run_app({"APP_MODE": "readonly", "ALLOC_MB": "8", "PORT": str(port)})
    deadline = time.monotonic() + 10
    while True:
        try:
            _get(port, "/healthz")
            break
        except OSError:
            if proc.poll() is not None or time.monotonic() > deadline:
                proc.kill()
                pytest.fail(f"app did not start: {proc.communicate()}")
            time.sleep(0.05)
    yield port, proc
    proc.terminate()
    proc.wait(timeout=10)


def test_invalid_app_mode_exits_with_status_2_and_logs_the_value() -> None:
    proc = _run_app({"APP_MODE": "invalid"})
    _, stderr = proc.communicate(timeout=10)
    assert proc.returncode == 2
    assert 'invalid APP_MODE "invalid"' in stderr


def test_health_endpoint_and_unknown_paths(running_app: tuple[int, subprocess.Popen[str]]) -> None:
    port, _ = running_app
    assert _get(port, "/healthz") == (200, b"ok\n")
    status, _ = _get(port, "/status")
    assert status == 404


def test_status_document_reports_mode_and_allocation(
    running_app: tuple[int, subprocess.Popen[str]],
) -> None:
    port, _ = running_app
    status, body = _get(port, "/")
    assert status == 200
    assert json.loads(body) == {"mode": "readonly", "allocated_mb": 8}


def test_sigterm_shuts_down_cleanly(running_app: tuple[int, subprocess.Popen[str]]) -> None:
    _, proc = running_app
    proc.terminate()
    stdout, _ = proc.communicate(timeout=10)
    assert proc.returncode == 0
    assert "allocated 8 MiB" in stdout
    assert "received SIGTERM" in stdout
