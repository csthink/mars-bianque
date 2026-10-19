"""The import-linter contracts fail when violated, instead of passing vacuously.

``contracts-negative.toml`` mirrors every forbidden contract of ``pyproject.toml`` onto
the fixture package ``bianque_negative_fixture``, which violates each one on purpose.
This test checks that the mirror is complete and that import-linter reports every
mirrored contract as BROKEN.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE_ROOT = Path(__file__).resolve().parent
NEGATIVE_CONFIG = BACKEND_ROOT / "contracts-negative.toml"
NEGATIVE_PREFIX = "Negative: "


def _contracts(path: Path) -> list[dict[str, Any]]:
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    contracts: list[dict[str, Any]] = config["tool"]["importlinter"]["contracts"]
    return contracts


def test_negative_config_mirrors_every_contract() -> None:
    positive = _contracts(BACKEND_ROOT / "pyproject.toml")
    negative = _contracts(NEGATIVE_CONFIG)
    assert positive, "pyproject.toml defines no import-linter contract"
    assert [NEGATIVE_PREFIX + c["name"] for c in positive] == [c["name"] for c in negative]
    for pos, neg in zip(positive, negative, strict=True):
        assert neg["type"] == pos["type"] == "forbidden"
        assert neg["forbidden_modules"] == pos["forbidden_modules"], neg["name"]
        assert neg["source_modules"] == ["bianque_negative_fixture"], neg["name"]


def test_every_negative_contract_is_broken() -> None:
    lint_imports = Path(sys.executable).with_name("lint-imports")
    assert lint_imports.exists(), f"lint-imports not found next to {sys.executable}"

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(FIXTURE_ROOT), env.get("PYTHONPATH", "")) if item
    )
    result = subprocess.run(
        [
            str(lint_imports),
            "--config",
            str(NEGATIVE_CONFIG),
            "--no-cache",
            "--no-logo",
        ],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout + result.stderr

    names = [c["name"] for c in _contracts(NEGATIVE_CONFIG)]
    assert result.returncode != 0, f"lint-imports passed on the negative fixture:\n{output}"
    for name in names:
        assert f"{name} BROKEN" in output, f"contract not reported BROKEN: {name}\n{output}"
    assert f"Contracts: 0 kept, {len(names)} broken." in output, output
