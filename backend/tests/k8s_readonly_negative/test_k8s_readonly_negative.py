"""The Kubernetes read-only check passes on the product and fails on the fixture.

Both runs use the same command line as the build: ``python -m tools.check_k8s_readonly``
from ``backend/``, without arguments for the positive check and with the fixture for
the negative one.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE = Path(__file__).resolve().parent / "bad_writes.py"


def _check(*paths: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tools.check_k8s_readonly", *paths],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_readonly_check_passes_on_the_package() -> None:
    result = _check()
    assert result.returncode == 0, result.stderr


def test_readonly_check_reports_every_write_path_in_the_fixture() -> None:
    result = _check(FIXTURE.relative_to(BACKEND_ROOT).as_posix())
    assert result.returncode == 1
    for expected in (
        "'delete_namespaced_pod'",
        "'patch_namespaced_pod_status'",
        "'connect_get_namespaced_pod_exec'",
        "names Kubernetes API method 'create_namespaced_pod'",
        "getattr with a computed name",
        "'call_api'",
        "'request'",
    ):
        assert expected in result.stderr, (expected, result.stderr)
