"""The checklists, the entry point and what a result may reveal."""

from __future__ import annotations

import json
import subprocess
import sys

from gate_cases import load_cases

from bianque.gate import GateContext, OutputType, checklist, validate

#: The validators of each output type.
DESIGN_TABLE = {
    OutputType.REPORT: [
        "schema",
        "citation_integrity",
        "knowledge_boundary",
        "unknown_discipline",
        "missing_evidence_consistency",
        "forbidden_actions",
        "redaction_residue",
    ],
    OutputType.FOLLOWUP_ANSWER: [
        "schema",
        "citation_integrity",
        "knowledge_boundary",
        "forbidden_actions",
        "redaction_residue",
    ],
    OutputType.PLAN: ["schema", "knowledge_boundary", "forbidden_actions", "redaction_residue"],
    OutputType.ASSESSMENT: ["schema", "forbidden_actions", "redaction_residue"],
    OutputType.FINDINGS: [
        "schema",
        "citation_integrity",
        "knowledge_boundary",
        "forbidden_actions",
        "redaction_residue",
    ],
}


def test_checklists_match_the_design_table() -> None:
    assert {t: checklist(t) for t in OutputType} == DESIGN_TABLE


def test_unregistered_output_type_is_rejected() -> None:
    result = validate("summary", {"text": "anything"}, GateContext())
    assert not result.passed
    assert result.output is None
    assert [(v.validator, v.code) for v in result.violations] == [
        ("checklist", "unregistered_type")
    ]


def test_schema_failure_stops_the_other_validators() -> None:
    result = validate(
        OutputType.FINDINGS,
        {"findings": [{"statement": "kubectl delete pod x", "citations": []}], "extra": 1},
        GateContext(),
    )
    assert result.validators() == ["schema"]


def test_parsed_model_and_json_give_the_same_result() -> None:
    for case in load_cases():
        if not case.passed:
            continue
        parsed = validate(case.output_type, case.output, case.context).output
        assert parsed is not None
        again = validate(case.output_type, parsed, case.context)
        assert again.passed and again.output == parsed, case.id


def test_rejection_never_repeats_the_output_text() -> None:
    for case in load_cases():
        if case.passed:
            continue
        result = validate(case.output_type, case.output, case.context)
        stored = json.dumps(result.to_json())
        for value in _strings(case.output):
            if len(value) > 24:
                assert value not in stored, (case.id, value)
        assert result.validators() == sorted({v[0] for v in case.violations}), case.id


def test_rejected_secrets_are_not_in_the_result() -> None:
    for case in load_cases():
        result = validate(case.output_type, case.output, case.context)
        stored = json.dumps(result.to_json())
        for fragment in ("ABCDEFabcdef0123456789", "IOSFODNN7EXAMPLE", "Passw0rd", "Sup3rSecret"):
            assert fragment not in stored, case.id


def test_result_json_shape() -> None:
    result = validate(OutputType.ASSESSMENT, {"sufficient": True}, GateContext())
    assert result.to_json() == {
        "output_type": "assessment",
        "passed": False,
        "violations": [
            {"validator": "schema", "path": "hypothesis_category", "code": "missing"},
            {"validator": "schema", "path": "missing", "code": "missing"},
            {"validator": "schema", "path": "reason", "code": "missing"},
        ],
    }


def test_the_gate_loads_without_the_model_gateway() -> None:
    """Importing the gate pulls in no module of ``bianque.llm``."""
    code = (
        "import sys, bianque.gate, bianque.agents.default_plan\n"
        "print(sorted(m for m in sys.modules if m.startswith(('bianque.llm', 'openai'))))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def _strings(data: object) -> list[str]:
    if isinstance(data, str):
        return [data]
    if isinstance(data, dict):
        return [s for value in data.values() for s in _strings(value)]
    if isinstance(data, list):
        return [s for value in data for s in _strings(value)]
    return []


def test_an_invented_key_does_not_reach_the_result() -> None:
    key = "".join(["ghp_", "ABCDEFabcdef0123456789", "ABCDEFabcd"])
    output = {"findings": [{"statement": "x", "citations": ["ev_0000000001"], key: 1}]}
    result = validate(OutputType.FINDINGS, output, GateContext())
    assert [(v.path, v.code) for v in result.violations] == [("findings[0].*", "extra_forbidden")]
    assert "ABCDEFabcdef0123456789" not in json.dumps(result.to_json())
