"""Every committed evidence pack loads and delivers its case's required evidence."""

from __future__ import annotations

import json

import pytest
from s1_evidence import check_required_evidence
from tool_fakes import CASES

from bianque.tools.backends.pack import open_pack
from bianque.tools.pack.format import load_meta

CASE_LETTERS = ["a", "b", "c", "d", "e", "f"]


@pytest.mark.parametrize("case", CASE_LETTERS)
async def test_the_pack_delivers_the_required_evidence(case: str) -> None:
    pack = CASES / f"s1-{case}" / "pack"
    meta = load_meta(pack)
    assert meta.case == f"s1-{case}"
    assert meta.namespace == "demo-s1"
    assert meta.source_case == ("s1-a" if case in ("d", "f") else None)
    alert = json.loads((pack / "alert.json").read_text())
    assert alert["status"] == "firing"
    assert alert["alerts"][0]["labels"]["alertname"] == "BianquePodRestarting"
    async with open_pack(pack) as backends:
        await check_required_evidence(backends, case, alert)


def test_the_recorded_repositories_carry_only_the_demo_identity() -> None:
    for case in ("a", "b", "c", "e"):
        text = (CASES / f"s1-{case}" / "pack" / "repo.fast-export").read_text()
        people = {
            line.split(">")[0] + ">"
            for line in text.splitlines()
            if line.startswith(("author ", "committer ", "tagger "))
        }
        assert {p.split(" ", 1)[1] for p in people} == {"Bianque Demo <demo@example.com>"}, case
