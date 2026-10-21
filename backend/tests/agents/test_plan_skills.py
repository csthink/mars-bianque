"""Skills while planning: the catalog, activation, deduplication, the limit and the records.

Scripted investigations of S1-a; a rule placed before the role rules records every
request, and some tests make the planning model call ``activate_skill`` its own way.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from investigation_harness import SKILLS, SKILLS_DIR, Investigated, investigate
from sqlalchemy import Engine

from bianque.llm import ScriptedReply
from bianque.llm.request import ModelRequest, request_key

Rule = Callable[[ModelRequest], ScriptedReply | None]

RESTARTS = "investigate-pod-restarts"
CONFIG = "diagnose-config-error"


def recorder(requests: list[ModelRequest]) -> Rule:
    def record(request: ModelRequest) -> ScriptedReply | None:
        requests.append(request)
        return None

    return record


def planning(requests: list[ModelRequest], operation: str = "plan") -> list[ModelRequest]:
    return [r for r in requests if r.agent == "investigation" and r.operation == operation]


def tool_names(request: ModelRequest) -> list[str]:
    return [tool.name for tool in request.tools]


def system(request: ModelRequest) -> str:
    return request.prompt.messages[0].content.text


def user(request: ModelRequest) -> str:
    return request.prompt.messages[1].content.text


def tool_results(request: ModelRequest) -> list[str]:
    return [m.content.text for m in request.prompt.messages if m.role == "tool"]


def activations(request: ModelRequest) -> int:
    return sum(
        1 for m in request.prompt.messages for c in m.tool_calls if c.name == "activate_skill"
    )


def activate(*names: str, operation: str = "plan") -> Rule:
    """Make the planning model activate ``names`` one per turn, then let the roles go on."""

    def rule(request: ModelRequest) -> ScriptedReply | None:
        if request.operation != operation or request.tool_choice != "required":
            return None
        done = activations(request)
        if done < len(names):
            return ScriptedReply(tool_calls=(("activate_skill", {"name": names[done]}),))
        return None

    return rule


def skill_records(result: Investigated) -> list[tuple[str, str]]:
    return [(c.name, c.output_summary or "") for c in result.calls if c.kind == "skill"]


async def test_the_plan_prompt_lists_the_catalog_and_no_body(migrated: Engine, pg_url: str) -> None:
    requests: list[ModelRequest] = []
    await investigate(pg_url, "s1-a", rules=[recorder(requests)])

    first = planning(requests)[0]
    assert first.tool_choice == "required"
    assert sorted(tool_names(first)) == ["activate_skill", "submit_plan"]
    activate_tool = next(t for t in first.tools if t.name == "activate_skill")
    assert activate_tool.parameters["properties"]["name"]["enum"] == list(SKILLS.names)

    catalog = system(first).split("Skill catalog:\n", 1)[1].splitlines()
    assert [line.split(":", 1)[0] for line in catalog] == [f"- {n}" for n in SKILLS.names]
    for skill in SKILLS:
        assert f"- {skill.name}: {skill.description}" in catalog
        body_line = skill.body.splitlines()[-1]
        assert body_line not in system(first) and body_line not in user(first)
    assert "## Skills" not in user(first)


async def test_an_activation_returns_the_body_and_is_recorded(
    migrated: Engine, pg_url: str
) -> None:
    requests: list[ModelRequest] = []
    result = await investigate(pg_url, "s1-a", rules=[recorder(requests)])

    second = planning(requests)[1]
    [content] = tool_results(second)
    restarts = SKILLS.get(RESTARTS)
    assert restarts is not None
    assert content.startswith(f'<skill_content name="{RESTARTS}">\n')
    assert restarts.body in content and "name: investigate-pod-restarts" not in content
    [(name, summary)] = skill_records(result)
    assert name == RESTARTS and summary.endswith("; first load")
    assert result.plans[0].skills == [RESTARTS]
    assert result.plans[0].source == "model"
    # Activations are no evidence.
    assert {e.source for e in result.evidence} <= {"alertmanager", "kubernetes", "git"}


async def test_a_name_outside_the_catalog_is_refused_and_not_recorded(
    migrated: Engine, pg_url: str
) -> None:
    requests: list[ModelRequest] = []
    result = await investigate(
        pg_url, "s1-a", rules=[recorder(requests), activate("restart-everything")]
    )

    second = planning(requests)[1]
    [refusal] = tool_results(second)
    assert refusal.startswith("Not loaded:") and RESTARTS in refusal
    assert "restart-everything" not in refusal
    assert skill_records(result) == []
    assert result.plans[0].skills == []


async def test_a_second_activation_only_says_already_loaded(migrated: Engine, pg_url: str) -> None:
    requests: list[ModelRequest] = []
    result = await investigate(
        pg_url, "s1-a", rules=[recorder(requests), activate(RESTARTS, RESTARTS)]
    )

    third = planning(requests)[2]
    first_result, second_result = tool_results(third)
    assert first_result.startswith("<skill_content")
    assert second_result == f"Skill {RESTARTS} is already loaded; its instructions are above."
    records = skill_records(result)
    assert [name for name, _ in records] == [RESTARTS, RESTARTS]
    assert records[0][1].endswith("; first load") and records[1][1].endswith("; already loaded")
    assert records[0][1].split(";")[0] == records[1][1].split(";")[0]
    assert result.plans[0].skills == [RESTARTS]


async def test_at_the_limit_only_submit_plan_is_offered(migrated: Engine, pg_url: str) -> None:
    requests: list[ModelRequest] = []
    names = (RESTARTS, CONFIG, RESTARTS)
    result = await investigate(
        pg_url,
        "s1-a",
        rules=[recorder(requests), activate(*names)],
        plan_max_skill_activations=2,
    )

    plans = planning(requests)
    assert [tool_names(r) for r in plans] == [
        ["activate_skill", "submit_plan"],
        ["activate_skill", "submit_plan"],
        ["submit_plan"],
    ]
    assert plans[-1].tool_choice == "submit_plan"
    assert [name for name, _ in skill_records(result)] == [RESTARTS, CONFIG]
    assert result.plans[0].skills == [CONFIG, RESTARTS]


async def test_without_skills_planning_is_one_forced_call(
    migrated: Engine, pg_url: str, tmp_path: Path
) -> None:
    for overrides in ({"skills_dir": tmp_path}, {"plan_max_skill_activations": 0}):
        requests: list[ModelRequest] = []
        result = await investigate(pg_url, "s1-a", rules=[recorder(requests)], **overrides)

        plans = [*planning(requests), *planning(requests, "replan")]
        assert plans and all(tool_names(r) == ["submit_plan"] for r in plans)
        assert all(r.tool_choice == "submit_plan" for r in plans)
        for request in plans:
            assert "Skill catalog" not in system(request) and "activate_skill" not in system(
                request
            )
            assert "## Skills" not in user(request)
        assert skill_records(result) == []
        assert all(p.skills == [] for p in result.plans)


async def test_the_supplementary_plan_holds_the_loaded_bodies_by_name(
    migrated: Engine, pg_url: str
) -> None:
    requests: list[ModelRequest] = []
    result = await investigate(
        pg_url, "s1-a", rules=[recorder(requests), activate(RESTARTS, CONFIG)]
    )

    replan = planning(requests, "replan")[0]
    skills = user(replan).split("## Skills\n", 1)[1]
    assert skills.index(f'<skill_content name="{CONFIG}">') < skills.index(
        f'<skill_content name="{RESTARTS}">'
    )
    # Indented, so a heading of a body is no section of the prompt.
    assert not any(line.startswith("## ") for line in skills.splitlines())
    # The supplementary plan may still load more.
    assert sorted(tool_names(replan)) == ["activate_skill", "submit_plan"]
    assert [p.skills for p in result.plans] == [[CONFIG, RESTARTS]] * 2


async def test_a_plan_submitted_in_the_loop_passes_the_gate_or_is_asked_again(
    migrated: Engine, pg_url: str
) -> None:
    requests: list[ModelRequest] = []

    def cite_the_skill(request: ModelRequest) -> ScriptedReply | None:
        if request.operation != "plan" or request.tool_choice != "required":
            return None
        plan = {
            "rationale": "Read the pod first.",
            "steps": [
                {
                    "goal": "Read the status of the restarting pod.",
                    "domain": "kubernetes",
                    "suggested_tools": ["k8s_get_pod_status"],
                    "expected_evidence": "The last termination.",
                }
            ],
            "knowledge_refs": [RESTARTS],
        }
        return ScriptedReply(tool_calls=(("submit_plan", plan),))

    result = await investigate(pg_url, "s1-a", rules=[recorder(requests), cite_the_skill])

    plans = planning(requests)
    assert [r.tool_choice for r in plans] == ["required", "submit_plan"]
    assert "skill_cited" in tool_results(plans[1])[-1]
    plan_calls = [c for c in result.calls if c.kind == "model" and c.name == "plan"]
    first_gate: dict[str, Any] = plan_calls[0].gate_result or {}
    assert {v["code"] for v in first_gate["violations"]} == {
        "skill_cited",
        "unknown_knowledge_id",
    }
    assert (plan_calls[1].gate_result or {})["passed"]
    assert result.plans[0].source == "model"


async def test_editing_a_skill_changes_the_request_keys(
    migrated: Engine, pg_url: str, tmp_path: Path
) -> None:
    keys: dict[str, list[str]] = {}
    for name, edit in (
        ("original", None),
        ("body", ("Use this skill first", "Use this skill at once")),
        ("description", ("Plan the investigation", "Plan an investigation")),
    ):
        target = tmp_path / name
        shutil.copytree(SKILLS_DIR, target)
        if edit is not None:
            path = target / RESTARTS / "SKILL.md"
            text = path.read_text()
            assert edit[0] in text
            path.write_text(text.replace(*edit, 1))
        requests: list[ModelRequest] = []
        await investigate(pg_url, "s1-a", rules=[recorder(requests)], skills_dir=target)
        keys[name] = [request_key(r) for r in planning(requests)]

    original = keys["original"]
    # The body arrives with the activation result: the first request is the same.
    assert keys["body"][0] == original[0] and keys["body"][1] != original[1]
    # The description is part of the catalog in the first request.
    assert keys["description"][0] != original[0]
