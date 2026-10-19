"""The environment variable prefix check passes on the product and fails on the fixture.

Both runs use the same command line as the build: ``python -m tools.check_env_prefix``
from ``backend/``, without arguments for the positive check and with this directory for
the negative one.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE_DIR = Path(__file__).resolve().parent


def _check(*paths: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tools.check_env_prefix", *paths],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_env_prefix_check_passes_on_config_package() -> None:
    result = _check()
    assert result.returncode == 0, result.stderr


def test_env_prefix_check_fails_on_negative_fixture() -> None:
    result = _check(FIXTURE_DIR.relative_to(BACKEND_ROOT).as_posix())
    assert result.returncode == 1, f"the check passed on the negative fixture:\n{result.stdout}"
    assert "'DATABASE_URL'" in result.stderr, result.stderr
    assert "bad_env.py" in result.stderr, result.stderr
