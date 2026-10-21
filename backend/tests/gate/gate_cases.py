"""Loader of the evidence gate sample set in ``cases/``.

A case file holds a list of cases of one output type; each case changes the base output
and context of that type in ``cases/bases/``. See the header of ``cases/report.yaml``.
Synthetic secrets are joined from fragments at load time, so no complete secret literal
appears in the repository.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from bianque.gate import FailedCall, GateContext, OutputType

CASES_DIR = Path(__file__).resolve().parent / "cases"

_SECRET_SLOT = "{secret}"


@dataclass(frozen=True)
class GateCase:
    """One sample: an output, its context and the expected result."""

    output_type: OutputType
    name: str
    output: dict[str, Any]
    context: GateContext
    passed: bool
    exercises: frozenset[str]
    violations: frozenset[tuple[str, str, str]]

    @property
    def id(self) -> str:
        return f"{self.output_type}/{self.name}"


def _parts(path: str) -> list[str | int]:
    return [int(part) if part.isdigit() else part for part in re.findall(r"[^.\[\]]+", path)]


def _parent(data: Any, path: str) -> tuple[Any, str | int]:
    parts = _parts(path)
    for part in parts[:-1]:
        data = data[part]
    return data, parts[-1]


def _fill_secret(data: Any, secret: str) -> Any:
    if isinstance(data, str):
        return data.replace(_SECRET_SLOT, secret)
    if isinstance(data, dict):
        return {key: _fill_secret(value, secret) for key, value in data.items()}
    if isinstance(data, list):
        return [_fill_secret(value, secret) for value in data]
    return data


def _context(data: dict[str, Any]) -> GateContext:
    return GateContext(
        evidence_ids=frozenset(data.get("evidence_ids") or ()),
        knowledge_ids=frozenset(data.get("knowledge_ids") or ()),
        suggestion_ids=frozenset(data.get("suggestion_ids") or ()),
        preserved=frozenset(data.get("preserved") or ()),
        failed_calls=tuple(FailedCall(**call) for call in data.get("failed_calls") or ()),
        skill_names=frozenset(data.get("skill_names") or ()),
    )


def load_cases() -> list[GateCase]:
    """Load every case of every output type."""
    cases = []
    for output_type in OutputType:
        base = yaml.safe_load((CASES_DIR / "bases" / f"{output_type}.yaml").read_text())
        for raw in yaml.safe_load((CASES_DIR / f"{output_type}.yaml").read_text()):
            output = copy.deepcopy(base["output"])
            for path, value in (raw.get("set") or {}).items():
                parent, key = _parent(output, path)
                parent[key] = copy.deepcopy(value)
            for path in raw.get("delete") or ():
                parent, key = _parent(output, path)
                del parent[key]
            if "secret_parts" in raw:
                output = _fill_secret(output, "".join(raw["secret_parts"]))
            expect = raw["expect"]
            cases.append(
                GateCase(
                    output_type=output_type,
                    name=raw["name"],
                    output=output,
                    context=_context({**base["context"], **(raw.get("context") or {})}),
                    passed=expect["passed"],
                    exercises=frozenset(expect.get("exercises") or ()),
                    violations=frozenset(tuple(item) for item in expect.get("violations") or ()),
                )
            )
    return cases
