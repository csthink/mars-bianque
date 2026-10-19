"""The sample set: every case gets exactly the expected result from ``validate``.

Each of the seven validators has at least one case it rejects and one passing case that
exercises it.
"""

from __future__ import annotations

import pytest
from gate_cases import GateCase, load_cases

from bianque.gate import CHECKLISTS, validate

CASES = load_cases()

VALIDATORS = {"schema"} | {
    validator.__name__ for checklist in CHECKLISTS.values() for validator in checklist
}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_case(case: GateCase) -> None:
    result = validate(case.output_type, case.output, case.context)
    found = {(v.validator, v.path, v.code) for v in result.violations}
    assert found == case.violations
    assert result.passed is case.passed
    assert (result.output is not None) is case.passed


def test_case_names_are_unique() -> None:
    assert len({case.id for case in CASES}) == len(CASES)


def test_every_validator_has_rejected_and_passing_cases() -> None:
    assert len(VALIDATORS) == 7
    rejecting = {validator for case in CASES for validator, _, _ in case.violations}
    exercising = {validator for case in CASES if case.passed for validator in case.exercises}
    assert rejecting == VALIDATORS
    assert exercising == VALIDATORS


def test_every_rejected_case_names_its_violations() -> None:
    for case in CASES:
        assert case.passed is not bool(case.violations), case.id
