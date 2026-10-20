"""Evaluation cases: the expected result of each case and its follow-up questions.

A case directory ``eval/cases/<case>/`` holds the evidence pack (``pack/``), the
annotation ``case.yaml`` and the question set ``questions.yaml``. Both files carry a
schema version; a file with another version, an unknown key, a tool that is not
registered or an argument that is not one of the tool's identity arguments fails to
load, and the error names the file.

Expected evidence is named by tool and identity arguments, not by evidence ID: an ID
also depends on the exact arguments a model passed (a short or a full commit hash), so
an annotation by ID could miss evidence the model did collect. A string argument may be
an ``fnmatch`` pattern (``s1-app-*``, ``617e33d*``).

The annotations are written from the evidence pack before any model output is read.
"""

from __future__ import annotations

from collections.abc import Mapping
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from bianque.gate.schemas import Confidence, RootCauseCategory
from bianque.tools import registry

#: Version of ``case.yaml``.
CASE_SCHEMA_VERSION = 1

#: Version of ``questions.yaml``. Version 2 names expected evidence by tool and identity
#: arguments; version 1 named evidence IDs.
QUESTIONS_SCHEMA_VERSION = 2

CASE_FILE = "case.yaml"
QUESTIONS_FILE = "questions.yaml"


class CaseFileError(ValueError):
    """A case file cannot be used; the message names the file."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceSpec(_Strict):
    """Evidence a tool collects, named by the tool and some of its identity arguments."""

    tool: str
    args: dict[str, str | bool | int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _registered_tool_and_identity_args(self) -> Self:
        definition = registry.get(self.tool)
        if definition is None or definition.kind != "evidence":
            raise ValueError(f"{self.tool!r} is not a registered evidence tool")
        unknown = sorted(set(self.args) - set(definition.identity_args))
        if unknown:
            raise ValueError(
                f"{self.tool}: {unknown} are not identity arguments "
                f"(those are {list(definition.identity_args)})"
            )
        return self

    def matches(self, tool: str | None, arguments: Mapping[str, Any]) -> bool:
        """Return whether evidence collected by ``tool`` with ``arguments`` is this one."""
        if tool != self.tool:
            return False
        for name, expected in self.args.items():
            actual = arguments.get(name)
            if isinstance(expected, str):
                if not isinstance(actual, str) or not fnmatchcase(actual, expected):
                    return False
            elif actual != expected or type(actual) is not type(expected):
                return False
        return True


class ExpectedObject(_Strict):
    """The Kubernetes object the root cause is in."""

    kind: str
    namespace: str
    name: str


class ExpectedRootCause(_Strict):
    """The root cause a correct report names."""

    status: Literal["identified", "unknown"]
    category: RootCauseCategory | None = None
    object: ExpectedObject | None = None

    @model_validator(mode="after")
    def _identified_names_category_and_object(self) -> Self:
        identified = self.status == "identified"
        if identified != (self.category is not None and self.object is not None):
            raise ValueError("identified names a category and an object; unknown names neither")
        return self


class Expected(_Strict):
    """What a correct investigation of the case reports."""

    root_cause: ExpectedRootCause
    #: Evidence the report as a whole should cite.
    required_evidence: tuple[EvidenceSpec, ...] = ()
    #: Evidence the root cause itself should cite.
    root_cause_evidence: tuple[EvidenceSpec, ...] = ()
    #: Confidence values the root cause must not have.
    confidence_not: tuple[Confidence, ...] = ()
    #: The missing evidence list should mention one of these words (case-insensitive).
    missing_evidence_mentions: tuple[str, ...] = ()


class Case(_Strict):
    """``case.yaml``."""

    schema_version: Literal[1]
    case: str
    title: str
    expected: Expected


class Question(_Strict):
    """One follow-up question and what a good answer contains."""

    id: str
    question: str = Field(min_length=1)
    #: Evidence a good answer cites at least one of.
    expected_evidence: tuple[EvidenceSpec, ...] = ()
    #: The earlier question this one refers back to ("it" is the commit of q1).
    depends_on: str | None = None
    #: A good answer cites a remediation suggestion.
    expected_suggestions: bool = False
    #: The evidence holds no answer: a good answer says the evidence is insufficient.
    expect_unknown: bool = False


class QuestionSet(_Strict):
    """``questions.yaml``: questions asked in this order in one session."""

    schema_version: Literal[2]
    questions: tuple[Question, ...]

    @model_validator(mode="after")
    def _ids_unique_and_references_earlier(self) -> Self:
        seen: set[str] = set()
        for question in self.questions:
            if question.id in seen:
                raise ValueError(f"question ID {question.id!r} repeats")
            earlier = question.depends_on
            if earlier is not None and earlier not in seen:
                raise ValueError(f"{question.id}: depends_on {earlier!r} is not asked before it")
            seen.add(question.id)
        return self


def _read(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CaseFileError(f"{path}: cannot read: {exc}") from exc


def _check_version(path: Path, data: Any, expected: int) -> None:
    version = data.get("schema_version") if isinstance(data, dict) else None
    if version != expected:
        raise CaseFileError(f"{path}: schema_version is {version!r}, expected {expected}")


def load_case(directory: Path) -> Case:
    """Load ``<directory>/case.yaml``.

    Raises:
        CaseFileError: The file is missing or invalid, or names another case.
    """
    path = directory / CASE_FILE
    data = _read(path)
    _check_version(path, data, CASE_SCHEMA_VERSION)
    try:
        case = Case.model_validate(data)
    except ValidationError as exc:
        raise CaseFileError(f"{path}: {exc}") from exc
    if case.case != directory.name:
        raise CaseFileError(f"{path}: case is {case.case!r}, expected {directory.name!r}")
    return case


def load_question_set(directory: Path) -> QuestionSet:
    """Load ``<directory>/questions.yaml``; a directory without one has no questions.

    Raises:
        CaseFileError: The file is invalid.
    """
    path = directory / QUESTIONS_FILE
    if not path.is_file():
        return QuestionSet(schema_version=2, questions=())
    data = _read(path)
    _check_version(path, data, QUESTIONS_SCHEMA_VERSION)
    try:
        return QuestionSet.model_validate(data)
    except ValidationError as exc:
        raise CaseFileError(f"{path}: {exc}") from exc


def case_directories(cases: Path) -> list[Path]:
    """Return the case directories under ``cases``: those with a ``case.yaml``, by name."""
    return sorted(path.parent for path in cases.glob(f"*/{CASE_FILE}"))
