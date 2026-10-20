"""The default plan and the evidence domain declaration it is built from."""

from __future__ import annotations

import re

from bianque.agents.default_plan import default_plan
from bianque.agents.domains import DOMAINS, domain
from bianque.gate import GateContext, OutputType, validate
from bianque.tools import registry


def test_first_round_covers_the_current_state_only() -> None:
    plan = default_plan(first_round=True)
    assert [step.domain for step in plan.steps] == ["kubernetes"] * 4 + ["logs"] * 2
    assert {step.domain for step in plan.steps} == {d.name for d in DOMAINS if d.first_round}


def test_supplementary_round_covers_the_changes() -> None:
    plan = default_plan(first_round=False)
    assert [step.domain for step in plan.steps] == ["change"] * 3


def test_default_plans_pass_the_plan_checklist() -> None:
    for first_round in (True, False):
        context = GateContext(preserved=frozenset(t.name for t in registry))
        result = validate(OutputType.PLAN, default_plan(first_round=first_round), context)
        assert result.violations == ()


def test_every_suggested_tool_is_an_evidence_tool_of_its_domain() -> None:
    for first_round in (True, False):
        for step in default_plan(first_round=first_round).steps:
            for name in step.suggested_tools:
                definition = registry.get(name)
                assert definition is not None, name
                assert definition.kind == "evidence"
                assert definition.domain == step.domain


def test_the_declared_domains_are_the_registry_domains() -> None:
    declared = {d.name for d in DOMAINS}
    registered = {t.domain for t in registry if t.kind == "evidence"}
    assert declared == registered


def test_default_plan_text_holds_no_time_or_identifier() -> None:
    text = (
        default_plan(first_round=True).model_dump_json()
        + default_plan(first_round=False).model_dump_json()
    )
    assert not re.search(r"\d{4}-\d{2}-\d{2}|inv_|ev_|kn_", text)
    assert default_plan(first_round=True) == default_plan(first_round=True)


def test_only_logs_failures_cap_the_confidence() -> None:
    assert [d.name for d in DOMAINS if d.failure_caps_confidence] == ["logs"]


def test_change_evidence_is_required_for_configuration_categories() -> None:
    assert domain("change").required_for_categories == {
        "config_error",
        "resource_limit",
        "probe_misconfig",
        "image_error",
    }


def test_tool_names_are_not_redaction_residue() -> None:
    """Tool names are words joined by ``_``: the entropy check passes them even unpreserved."""
    result = validate(OutputType.PLAN, default_plan(first_round=False), GateContext())
    assert result.passed, result.validators()
