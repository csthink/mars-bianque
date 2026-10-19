"""The output schemas."""

from __future__ import annotations

import typing

import pytest
from pydantic import ValidationError

from bianque.gate import SCHEMA_VERSION, SCHEMAS, OutputType, Report, RootCause
from bianque.gate.checklists import CHECKLISTS


def test_every_output_type_has_a_schema_and_a_checklist() -> None:
    assert set(SCHEMAS) == set(OutputType) == set(CHECKLISTS)


def test_schema_version_is_the_only_allowed_value() -> None:
    assert typing.get_args(Report.model_fields["schema_version"].annotation) == (SCHEMA_VERSION,)


def test_root_cause_categories_are_the_design_enumeration() -> None:
    annotation = RootCause.model_fields["category"].annotation
    (literal,) = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
    assert set(typing.get_args(literal)) == {
        "config_error",
        "resource_limit",
        "probe_misconfig",
        "image_error",
        "dependency_unavailable",
        "code_defect",
        "other",
    }


def test_every_report_section_is_required() -> None:
    required = {name for name, field in Report.model_fields.items() if field.is_required()}
    assert required == set(Report.model_fields)


def test_identified_root_cause_needs_category_and_object() -> None:
    with pytest.raises(ValidationError):
        RootCause(
            status="identified",
            category="config_error",
            statement="x",
            citations=["ev_0000000001"],
            confidence="medium",
        )
    unknown = RootCause(status="unknown", statement="x", citations=[], confidence="low")
    assert unknown.category is None and unknown.object is None


def test_outputs_are_immutable() -> None:
    root_cause = RootCause(status="unknown", statement="x", citations=[], confidence="low")
    with pytest.raises(ValidationError):
        root_cause.confidence = "high"
