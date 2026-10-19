"""The required evidence of each S1 case, checked through the tools.

Shared by the evidence pack tests (replay backend) and the kind integration tests (real
backend): the same calls must deliver the same evidence from both. Every call goes
through the executor, so the checks read what a model would read.
"""

from __future__ import annotations

import json
from typing import Any

from bianque.redaction import build_engine
from bianque.tools import CallerScope, ToolExecutor, ToolResult
from bianque.tools.backends.base import ToolBackends
from bianque.tools.store import EvidenceRecord, ToolCallRecord

NAMESPACE = "demo-s1"
DEPLOYMENT = "s1-app"


class _NoSink:
    async def save(self, call: ToolCallRecord, evidence: EvidenceRecord | None) -> None:
        return None


class Tools:
    """Calls tools the way an evidence sub-agent of the tool's domain would."""

    def __init__(self, backends: ToolBackends) -> None:
        self._executor = ToolExecutor(backends, _NoSink(), engine=build_engine())

    async def result(self, tool: str, /, **args: Any) -> ToolResult:
        """Call a tool and return the full result."""
        domain = "kubernetes" if tool.startswith("k8s_") else tool.split("_", 1)[0]
        return await self._executor.call(tool, args, scope=CallerScope(agent=domain, domain=domain))

    async def data(self, tool: str, /, **args: Any) -> dict[str, Any]:
        """Call a tool that must succeed and return its parsed output."""
        result = await self.result(tool, **args)
        assert result.status == "ok", (tool, args, result.error)
        data: dict[str, Any] = json.loads(result.model_view.text)
        return data


def changed(diff: str, sign: str, text: str) -> bool:
    """Return whether a diff removes (``-``) or adds (``+``) a line containing ``text``."""
    return any(
        line.startswith(sign) and not line.startswith(sign * 3) and text in line
        for line in diff.splitlines()
    )


async def check_required_evidence(backends: ToolBackends, case: str, alert: dict[str, Any]) -> None:
    """Assert that the tools deliver the required evidence of one S1 case.

    Args:
        backends: Real backends while the case is injected, or the case's evidence pack.
        case: ``a`` to ``f``.
        alert: The Alertmanager webhook body of the case; it names the failing pod.
    """
    labels = alert["alerts"][0]["labels"]
    pod, container = labels["pod"], labels["container"]
    tools = Tools(backends)

    status = await tools.data("k8s_get_pod_status", namespace=NAMESPACE, pod=pod)
    app = next(c for c in status["containers"] if c["name"] == container)
    assert app["restart_count"] >= 1
    if case == "f":
        listed = await tools.data("k8s_list_pods", namespace=NAMESPACE)
        assert pod in [row["name"] for row in listed["pods"]]
        for name, args in (
            ("k8s_list_events", {"namespace": NAMESPACE}),
            ("k8s_get_deployment", {"namespace": NAMESPACE, "name": DEPLOYMENT}),
            ("change_get_rollout_history", {"namespace": NAMESPACE, "deployment": DEPLOYMENT}),
            ("change_list_commits", {}),
            (
                "logs_get_container_logs",
                {"namespace": NAMESPACE, "pod": pod, "container": container, "previous": True},
            ),
        ):
            result = await tools.result(name, **args)
            assert result.status == "error" and result.error is not None, name
            assert result.error.startswith("not_found: "), (name, result.error)
        return

    deployment = await tools.data("k8s_get_deployment", namespace=NAMESPACE, name=DEPLOYMENT)
    spec = deployment["containers"][0]
    env = {item["name"]: item.get("value") for item in spec["env"]}
    history = await tools.data(
        "change_get_rollout_history", namespace=NAMESPACE, deployment=DEPLOYMENT
    )
    assert len(history["revisions"]) == 2
    commits = (await tools.data("change_list_commits"))["commits"]

    if case in ("a", "d", "e"):
        assert app["last_state"]["exit_code"] == 2
        logs = await tools.result(
            "logs_get_container_logs",
            namespace=NAMESPACE,
            pod=pod,
            container=container,
            previous=True,
        )
        if case == "d":
            assert logs.status == "error"
            assert logs.error == (
                f"timeout: logs_get_container_logs namespace={NAMESPACE} pod={pod} "
                f"container={container} previous=True"
            )
        else:
            assert logs.status == "ok", logs.error
            assert any(
                "invalid APP_MODE" in line for line in json.loads(logs.model_view.text)["lines"]
            )
        assert env["APP_MODE"] == "invalid"
        # S1-e adds a newer commit that only touches the notes; the fault is older.
        if case == "e":
            assert commits[0]["files"] == ["README.md"]
            fault = commits[1]
        else:
            fault = commits[0]
        shown = await tools.data("change_show_commit", commit=fault["commit"])
        assert "APP_MODE" in shown["diff"]
        assert changed(shown["diff"], "-", "value: standard")
        assert changed(shown["diff"], "+", "value: invalid")
    elif case == "b":
        assert app["last_state"]["reason"] == "OOMKilled"
        assert spec["resources"]["limits"]["memory"] == "32Mi"
        patch = history["template_diff"]["patch"]
        assert changed(patch, "-", '"memory": "128Mi"')
        assert changed(patch, "+", '"memory": "32Mi"')
    else:
        events = await tools.data("k8s_list_events", namespace=NAMESPACE, involved_object=pod)
        assert any(
            e["reason"] == "Unhealthy" and "Liveness probe failed" in e["message"]
            for e in events["events"]
        )
        assert spec["liveness_probe"]["httpGet"]["path"] == "/status"
        shown = await tools.data("change_show_commit", commit=commits[0]["commit"])
        assert changed(shown["diff"], "+", "path: /status")
