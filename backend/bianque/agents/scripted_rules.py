"""Rules that play the agent roles in ``scripted`` mode.

Scripted mode runs investigations without a model, for tests and for the end-to-end
tests of the replay image. These rules answer each call site the way a careful model
would, reading only the request: the sections of the user message
(``bianque.agents.prompting``), the tool results in the conversation and the offered
tools. They never read the database and never name an evaluation case.

- ``plan``: the first-round default steps for the pod named in the alert.
- ``evidence_agent``: call the step's suggested tools one by one with arguments taken
  from the alert and from earlier results, then submit one finding per evidence ID.
- ``assess``: sufficient once there is pod status and change evidence.
- ``replan``: the default steps of the domains that have no evidence yet.
- ``report``: a root cause when a change diff and the pod status support the category
  the evidence points to; unknown otherwise. Every failed call is listed as missing
  evidence, and a failed logs call lowers the confidence.

Plans and reports refer to the knowledge chunk with the smallest ID when the request
offers any, so replays cover knowledge in its one allowed place.

The heuristics read Kubernetes and Git evidence; a new domain brings its own rules.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from bianque.agents.domains import DOMAINS, POD_STATUS_TOOLS, domain
from bianque.agents.prompting import (
    EVIDENCE,
    FAILED_CALLS,
    KNOWLEDGE,
    STEP,
)
from bianque.gate import SCHEMA_VERSION
from bianque.llm import ScriptedReply, ScriptedRule
from bianque.llm.request import ModelRequest
from bianque.tools import registry

_HEADER = re.compile(r"^\[(ev_[0-9a-f]{10})\] source: (\S+); tool: (\S+)$")

#: A knowledge entry: ``[kn_…] title`` in plans, ``- kn_…: title`` in reports.
_KNOWLEDGE_ENTRY = re.compile(r"^(?:\[(kn_[0-9a-f]{12})\] |- (kn_[0-9a-f]{12}): )(.+)$")
_FAILURE = re.compile(r"^- (.+?) \((\w+) domain\): (.+)$")
_REPLICASET_POD = re.compile(r"^(?P<workload>.+)-[0-9a-f]{6,10}-[0-9a-z]{5}$")
_EVIDENCE_ID = re.compile(r"^Evidence ID: (ev_[0-9a-f]{10}(?:, ev_[0-9a-f]{10})*)$", re.M)


@dataclass(frozen=True)
class _Evidence:
    id: str
    source: str
    tool: str | None
    text: str

    def data(self) -> Any:
        try:
            return json.loads(self.text)
        except json.JSONDecodeError:
            return None


@dataclass(frozen=True)
class _Result:
    """A tool call of the conversation and what it returned."""

    name: str
    arguments: dict[str, Any]
    evidence_ids: tuple[str, ...]
    data: Any


def _sections(request: ModelRequest) -> dict[str, str]:
    """Split the first user message into its sections by heading."""
    user = next((m.content.text for m in request.prompt.messages if m.role == "user"), "")
    sections: dict[str, str] = {}
    heading: str | None = None
    lines: list[str] = []
    for line in user.splitlines():
        if line.startswith("## "):
            if heading is not None:
                sections[heading] = "\n".join(lines).strip()
            heading, lines = line[3:].strip(), []
        else:
            lines.append(line)
    if heading is not None:
        sections[heading] = "\n".join(lines).strip()
    return sections


def _knowledge(sections: Mapping[str, str]) -> list[tuple[str, str]]:
    """The knowledge entries of the request as ``(id, title)``, sorted by ID."""
    entries = []
    for line in sections.get(KNOWLEDGE, "").splitlines():
        match = _KNOWLEDGE_ENTRY.match(line)
        if match:
            entries.append((match.group(1) or match.group(2), match.group(3)))
    return sorted(entries)


def _knowledge_refs(sections: Mapping[str, str]) -> list[str]:
    return [knowledge_id for knowledge_id, _ in _knowledge(sections)[:1]]


def _evidence(sections: Mapping[str, str]) -> list[_Evidence]:
    items: list[_Evidence] = []
    header: tuple[str, str, str | None] | None = None
    lines: list[str] = []
    for line in sections.get(EVIDENCE, "").splitlines():
        match = _HEADER.match(line)
        if match:
            if header is not None:
                items.append(_Evidence(*header, "\n".join(lines).strip()))
            tool = match.group(3)
            header, lines = (match.group(1), match.group(2), None if tool == "-" else tool), []
        else:
            lines.append(line)
    if header is not None:
        items.append(_Evidence(*header, "\n".join(lines).strip()))
    return items


def _results(request: ModelRequest) -> list[_Result]:
    """Return the tool calls of the conversation with their results, in call order."""
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    results: list[_Result] = []
    for message in request.prompt.messages:
        for call in message.tool_calls:
            try:
                arguments = json.loads(call.arguments.text)
            except json.JSONDecodeError:
                arguments = {}
            calls[call.id] = (call.name, arguments if isinstance(arguments, dict) else {})
        if message.role == "tool" and message.tool_call_id in calls:
            name, arguments = calls[message.tool_call_id]
            text = message.content.text
            match = _EVIDENCE_ID.search(text)
            ids = tuple(match.group(1).split(", ")) if match else ()
            body = text.split("\n", 1)[1] if match and "\n" in text else ""
            try:
                data = json.loads(body) if body else None
            except json.JSONDecodeError:
                data = None
            results.append(_Result(name, arguments, ids, data))
    return results


def _labels(evidence: Iterable[_Evidence]) -> dict[str, str]:
    for item in evidence:
        if item.source == "alertmanager":
            data = item.data()
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in (data.get("labels") or {}).items()}
    return {}


def _workload(labels: Mapping[str, str]) -> str:
    if labels.get("deployment"):
        return labels["deployment"]
    pod = labels.get("pod", "")
    match = _REPLICASET_POD.match(pod)
    return match.group("workload") if match else pod


def _domain_of(tool: str | None) -> str | None:
    definition = registry.get(tool) if tool else None
    return definition.domain if definition else None


def _changed_lines(diff: str) -> list[str]:
    return [
        line
        for line in diff.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]


def _diff_of(item: _Evidence) -> str:
    data = item.data()
    if not isinstance(data, dict):
        return ""
    if item.tool == "change_show_commit":
        return str(data.get("diff") or "")
    if item.tool == "change_get_rollout_history":
        return str((data.get("template_diff") or {}).get("patch") or "")
    return ""


def _category(evidence: Iterable[_Evidence]) -> str | None:
    """Return the root cause category the evidence points to, if any."""
    items = list(evidence)
    texts = [item.text for item in items if item.source != "alertmanager"]
    if any("OOMKilled" in text for text in texts):
        return "resource_limit"
    if any("Liveness probe failed" in text for text in texts):
        return "probe_misconfig"
    diffs = "\n".join(line for item in items for line in _changed_lines(_diff_of(item)))
    if "value:" in diffs:
        return "config_error"
    if any(item.tool == "logs_get_container_logs" and "invalid" in item.text for item in items):
        return "config_error"
    return None


#: The field of the workload's container each category points to.
_FIELDS = {
    "resource_limit": "resources.limits.memory",
    "probe_misconfig": "livenessProbe",
}


def _field(category: str, diff: str, container: str) -> str:
    base = f"spec.template.spec.containers[{container}]"
    if category in _FIELDS:
        return f"{base}.{_FIELDS[category]}"
    name = None
    for line in diff.splitlines():
        if match := re.search(r"- name: (\S+)", line):
            name = match.group(1)
        if line.startswith("+") and "value:" in line and name:
            return f"{base}.env[{name}]"
    return base


def _call(name: str, arguments: Mapping[str, Any]) -> ScriptedReply:
    return ScriptedReply(tool_calls=((name, dict(arguments)),))


def _plan_steps(names: Iterable[str]) -> list[dict[str, Any]]:
    return [
        {
            "goal": template.goal,
            "domain": item.name,
            "suggested_tools": list(template.suggested_tools),
            "expected_evidence": template.expected_evidence,
        }
        for name in names
        for item in (domain(name),)
        for template in item.default_steps
    ]


def plan_rule(request: ModelRequest) -> ScriptedReply | None:
    """The first plan: the first-round default steps for the alerted pod."""
    if request.operation != "plan":
        return None
    sections = _sections(request)
    labels = _labels(_evidence(sections))
    names = [item.name for item in DOMAINS if item.first_round]
    rationale = (
        f"The alert names pod {labels.get('pod', 'unknown')} in namespace "
        f"{labels.get('namespace', 'unknown')}. Read its current state and its logs first."
    )
    return _call(
        "submit_plan",
        {
            "rationale": rationale,
            "steps": _plan_steps(names),
            "knowledge_refs": _knowledge_refs(sections),
        },
    )


def replan_rule(request: ModelRequest) -> ScriptedReply | None:
    """A supplementary plan: the default steps of the domains without evidence."""
    if request.operation != "replan":
        return None
    sections = _sections(request)
    covered = {_domain_of(item.tool) for item in _evidence(sections)}
    names = [item.name for item in DOMAINS if item.name not in covered] or [
        item.name for item in DOMAINS if not item.first_round
    ]
    rationale = "Find the change that introduced the failure: " + ", ".join(names) + "."
    return _call(
        "submit_plan",
        {
            "rationale": rationale,
            "steps": _plan_steps(names),
            "knowledge_refs": _knowledge_refs(sections),
        },
    )


def _arguments(
    tool: str, labels: Mapping[str, str], goal: str, results: list[_Result]
) -> dict[str, Any] | None:
    namespace, pod = labels.get("namespace", ""), labels.get("pod", "")
    container, workload = labels.get("container", ""), _workload(labels)
    if tool == "k8s_list_pods":
        return {"namespace": namespace}
    if tool in ("k8s_get_pod_status",):
        return {"namespace": namespace, "pod": pod}
    if tool == "k8s_list_events":
        return {"namespace": namespace}
    if tool == "k8s_get_deployment":
        return {"namespace": namespace, "name": workload}
    if tool == "logs_get_container_logs":
        previous = "previous" in goal.lower()
        return {"namespace": namespace, "pod": pod, "container": container, "previous": previous}
    if tool == "change_get_rollout_history":
        return {"namespace": namespace, "deployment": workload}
    if tool == "change_list_commits":
        return {}
    if tool == "change_show_commit":
        listed = [r.data for r in results if r.name == "change_list_commits" and r.data]
        if not listed:
            return None
        commits = listed[-1].get("commits") or []
        touching = [c for c in commits if any("deployment" in f for f in c.get("files") or [])]
        chosen = (touching or commits or [None])[0]
        return {"commit": chosen["commit"]} if chosen else None
    return None


def evidence_agent_rule(request: ModelRequest) -> ScriptedReply | None:
    """Call the step's tools in order, then submit one finding per evidence ID."""
    if request.operation != "evidence_agent":
        return None
    results = _results(request)
    findings = [
        {"statement": f"{result.name} returned evidence for this step.", "citations": [eid]}
        for result in results
        for eid in result.evidence_ids
    ]
    submit = ("submit_findings", {"findings": findings})
    if request.tool_choice == "submit_findings":
        return ScriptedReply(tool_calls=(submit,))
    sections = _sections(request)
    labels = _labels(_evidence(sections))
    step = sections.get(STEP, "")
    goal = next((line[6:] for line in step.splitlines() if line.startswith("Goal: ")), "")
    suggested = next(
        (
            line[17:].split(", ")
            for line in step.splitlines()
            if line.startswith("Suggested tools: ")
        ),
        [],
    )
    offered = {tool.name for tool in request.tools}
    called = {result.name for result in results}
    for tool in suggested:
        if tool not in offered or tool in called:
            continue
        arguments = _arguments(tool, labels, goal, results)
        if arguments is None and tool == "change_show_commit" and "change_list_commits" in offered:
            if "change_list_commits" not in called:
                return _call("change_list_commits", {})
            continue
        if arguments is not None:
            return _call(tool, arguments)
    return ScriptedReply(tool_calls=(submit,))


def assess_rule(request: ModelRequest) -> ScriptedReply | None:
    """Sufficient once the pod status and evidence of the change domain are in."""
    if request.operation != "assess":
        return None
    evidence = _evidence(_sections(request))
    has_status = any(item.tool in POD_STATUS_TOOLS for item in evidence)
    has_change = any(_domain_of(item.tool) == "change" for item in evidence)
    sufficient = has_status and has_change
    missing = (
        []
        if sufficient
        else ["The change that introduced the failure: rollout history and commits."]
    )
    reason = (
        "The pod status and the change that introduced the failure are both in the evidence."
        if sufficient
        else "The current state is known, but the change that introduced it is not."
    )
    return _call(
        "submit_assessment",
        {
            "sufficient": sufficient,
            "reason": reason,
            "hypothesis_category": _category(evidence),
            "missing": missing,
        },
    )


def _failures(sections: Mapping[str, str]) -> list[tuple[str, str, str]]:
    rows = []
    for line in sections.get(FAILED_CALLS, "").splitlines():
        if match := _FAILURE.match(line):
            rows.append((match.group(1), match.group(2), match.group(3)))
    return rows


def _status_claim(item: _Evidence) -> dict[str, Any] | None:
    data = item.data()
    if not isinstance(data, dict):
        return None
    for container in data.get("containers") or []:
        last = container.get("last_state") or {}
        if last.get("reason"):
            return {
                "statement": f"Container {container.get('name')} of pod {data.get('name')} "
                f"restarted {container.get('restart_count')} times; its last termination "
                f"reason was {last.get('reason')} with exit code {last.get('exit_code')}.",
                "citations": [item.id],
            }
    return None


def report_rule(request: ModelRequest) -> ScriptedReply | None:
    """A report whose root cause is supported by a change diff and the pod status."""
    if request.operation != "report":
        return None
    sections = _sections(request)
    evidence = _evidence(sections)
    labels = _labels(evidence)
    namespace, pod = labels.get("namespace", "unknown"), labels.get("pod", "unknown")
    container, workload = labels.get("container", "unknown"), _workload(labels)
    alert = next((item for item in evidence if item.source == "alertmanager"), None)
    status = next((item for item in evidence if item.tool in POD_STATUS_TOOLS), None)
    diffs = [item for item in evidence if _changed_lines(_diff_of(item))]
    commits = [item for item in diffs if item.tool == "change_show_commit"]
    diff = next(iter(commits or diffs), None)
    logs = [item for item in evidence if item.tool == "logs_get_container_logs"]
    category = _category(evidence)
    failures = _failures(sections)
    caps = any(row[1] == "logs" for row in failures)

    timeline = []
    if diff is not None and diff.tool == "change_show_commit":
        data = diff.data()
        timeline.append(
            {
                "time": data["date"],
                "event": f"Commit {data['short']} changed the manifest of {workload}.",
                "citations": [diff.id],
            }
        )
    if alert is not None:
        starts_at = (alert.data() or {}).get("startsAt")
        if starts_at:
            timeline.append(
                {
                    "time": starts_at,
                    "event": f"Alert {labels.get('alertname', '')} fired for pod {pod}.",
                    "citations": [alert.id],
                }
            )
    status_claim = _status_claim(status) if status is not None else None
    claims = [status_claim] if status_claim is not None else []
    claims += [
        {"statement": f"The logs of container {container} were read.", "citations": [item.id]}
        for item in logs
    ]

    identified = category is not None and diff is not None and status is not None
    if identified:
        assert category is not None and diff is not None and status is not None
        field = _field(category, _diff_of(diff), container)
        data = diff.data() or {}
        change = f"commit {data['short']}" if diff.tool == "change_show_commit" else "the rollout"
        citations = sorted(
            {diff.id, status.id, *(item.id for item in logs if category == "config_error")}
        )
        root_cause: dict[str, Any] = {
            "status": "identified",
            "category": category,
            "object": {
                "kind": "Deployment",
                "namespace": namespace,
                "name": workload,
                "field": field,
            },
            "statement": f"{change} changed {field} of deployment {workload}, and the container "
            "has failed since.",
            "citations": citations,
            "confidence": "medium" if caps else "high",
        }
        summary = (
            f"Pod {pod} in {namespace} keeps restarting: {category} in deployment {workload}, "
            f"introduced by {change}."
        )
        suggestions: list[dict[str, Any]] = [
            {
                "action": f"Revert {change} in the manifest repository and roll out again.",
                "command": f"git revert {data['short']}" if "short" in data else None,
                "risk": "change",
                "citations": [diff.id],
            },
            {
                "action": "Check that the pod stops restarting after the rollout.",
                "command": f"kubectl get pod {pod} -n {namespace}",
                "risk": "read_only",
                "citations": [status.id],
            },
        ]
    else:
        root_cause = {
            "status": "unknown",
            "statement": "The evidence does not establish the root cause.",
            "citations": [],
            "confidence": "low",
        }
        summary = f"Pod {pod} in {namespace} keeps restarting; the evidence does not show why."
        suggestions = []

    body = {
        "schema_version": SCHEMA_VERSION,
        "summary": summary,
        "impact": f"Deployment {workload} has a pod that is not ready while it restarts.",
        "timeline": sorted(timeline, key=lambda entry: entry["time"]),
        "root_cause": root_cause,
        "claims": claims,
        "missing_evidence": [
            {
                "what": f"Result of {tool} ({domain_name} domain).",
                "reason": f"The call failed: {error}.",
            }
            for tool, domain_name, error in failures
        ],
        "suggestions": suggestions,
        "references": [
            {"knowledge_id": knowledge_id, "title": title}
            for knowledge_id, title in _knowledge(sections)[:1]
        ],
        "followup_questions": [f"What else changed in deployment {workload} recently?"],
    }
    return _call("submit_report", body)


#: The role rules, tried after any rules a test injects.
ROLE_RULES: tuple[ScriptedRule, ...] = (
    plan_rule,
    replan_rule,
    evidence_agent_rule,
    assess_rule,
    report_rule,
)
