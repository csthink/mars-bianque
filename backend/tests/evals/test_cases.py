"""Case files: every case loads, and a malformed file fails naming itself."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from bianque.evals.cases import (
    CaseFileError,
    EvidenceSpec,
    case_directories,
    load_case,
    load_question_set,
)

CASES = Path(__file__).resolve().parents[3] / "eval" / "cases"

_CASE: dict[str, Any] = {
    "schema_version": 1,
    "case": "s1-x",
    "title": "A case",
    "expected": {
        "root_cause": {
            "status": "identified",
            "category": "config_error",
            "object": {"kind": "Deployment", "namespace": "demo-s1", "name": "s1-app"},
        },
        "required_evidence": [{"tool": "k8s_get_deployment", "args": {"name": "s1-app"}}],
    },
}


def _write(tmp_path: Path, name: str, data: Any) -> Path:
    directory = tmp_path / "s1-x"
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(yaml.safe_dump(data, allow_unicode=True))
    return directory


def test_every_case_and_question_set_loads() -> None:
    directories = case_directories(CASES)
    assert [d.name for d in directories] == ["s1-a", "s1-b", "s1-c", "s1-d", "s1-e", "s1-f"]
    for directory in directories:
        case = load_case(directory)
        assert (directory / "pack" / "alert.json").is_file()
        assert load_question_set(directory).questions, case.case
    assert load_case(CASES / "s1-f").expected.root_cause.status == "unknown"


def test_every_question_set_has_an_unknown_question_and_s1a_a_follow_on() -> None:
    for directory in case_directories(CASES):
        assert any(q.expect_unknown for q in load_question_set(directory).questions)
    assert any(q.depends_on for q in load_question_set(CASES / "s1-a").questions)


def test_a_valid_case_loads(tmp_path: Path) -> None:
    case = load_case(_write(tmp_path, "case.yaml", _CASE))
    assert case.expected.required_evidence[0].tool == "k8s_get_deployment"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"schema_version": 2}, "schema_version is 2, expected 1"),
        ({"case": "s1-y"}, "expected 's1-x'"),
        ({"extra": 1}, "extra"),
        (
            {"expected": {"root_cause": {"status": "identified"}}},
            "identified names a category and an object",
        ),
        (
            {
                "expected": {
                    "root_cause": {"status": "unknown"},
                    "required_evidence": [{"tool": "k8s_delete_pod"}],
                }
            },
            "not a registered evidence tool",
        ),
        (
            {
                "expected": {
                    "root_cause": {"status": "unknown"},
                    "required_evidence": [
                        {"tool": "logs_get_container_logs", "args": {"tail_lines": 50}}
                    ],
                }
            },
            "are not identity arguments",
        ),
        (
            {
                "expected": {
                    "root_cause": {"status": "unknown"},
                    "required_evidance": [],
                }
            },
            "required_evidance",
        ),
    ],
)
def test_a_malformed_case_fails_naming_the_file(
    tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    directory = _write(tmp_path, "case.yaml", {**_CASE, **change})
    with pytest.raises(CaseFileError) as error:
        load_case(directory)
    assert str(directory / "case.yaml") in str(error.value)
    assert message in str(error.value)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"schema_version": 1, "questions": []}, "schema_version is 1, expected 2"),
        (
            {"schema_version": 2, "questions": [{"id": "q1", "question": "A?", "expect": True}]},
            "expect",
        ),
        (
            {
                "schema_version": 2,
                "questions": [{"id": "q1", "question": "A?", "depends_on": "q2"}],
            },
            "is not asked before it",
        ),
        (
            {
                "schema_version": 2,
                "questions": [{"id": "q1", "question": "A?"}, {"id": "q1", "question": "B?"}],
            },
            "repeats",
        ),
    ],
)
def test_a_malformed_question_set_fails_naming_the_file(
    tmp_path: Path, data: dict[str, Any], message: str
) -> None:
    directory = _write(tmp_path, "questions.yaml", data)
    with pytest.raises(CaseFileError) as error:
        load_question_set(directory)
    assert str(directory / "questions.yaml") in str(error.value)
    assert message in str(error.value)


def test_a_case_without_questions_has_none(tmp_path: Path) -> None:
    assert load_question_set(tmp_path).questions == ()


def test_evidence_matching_by_tool_and_identity_arguments() -> None:
    commit = EvidenceSpec(tool="change_show_commit", args={"commit": "617e33d*"})
    assert commit.matches("change_show_commit", {"commit": "617e33d62ee7671a"})
    assert commit.matches("change_show_commit", {"commit": "617e33d"})
    assert not commit.matches("change_show_commit", {"commit": "0b11d9fd18f8"})
    assert not commit.matches("k8s_get_deployment", {"commit": "617e33d"})

    logs = EvidenceSpec(tool="logs_get_container_logs", args={"previous": True})
    assert logs.matches("logs_get_container_logs", {"previous": True, "pod": "p"})
    assert not logs.matches("logs_get_container_logs", {"previous": False})
    assert not logs.matches("logs_get_container_logs", {"previous": 1})
    assert not logs.matches("logs_get_container_logs", {})
