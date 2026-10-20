"""Evidence tools of the ``kubernetes`` domain: the current state of pods and deployments."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pydantic import Field

from bianque.tools.backends.base import KubeObject, ToolBackends
from bianque.tools.fields import (
    LabelSelector,
    Namespace,
    ObjectName,
    ToolArgs,
    format_time,
    metadata,
    owner,
    parse_time,
    status_words,
)
from bianque.tools.output import ToolOutput, collect_identifiers, render
from bianque.tools.registry import tool

#: The newest events kept by ``k8s_list_events``.
MAX_EVENTS = 50

_TIMEOUT_S = 10


def container_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    """Render one ``state`` or ``lastState`` entry of a container status."""
    if not state:
        return None
    if waiting := state.get("waiting"):
        return {
            "state": "waiting",
            "reason": waiting.get("reason"),
            "message": waiting.get("message"),
        }
    if running := state.get("running"):
        return {"state": "running", "started_at": format_time(running.get("startedAt"))}
    if terminated := state.get("terminated"):
        return {
            "state": "terminated",
            "reason": terminated.get("reason"),
            "exit_code": terminated.get("exitCode"),
            "started_at": format_time(terminated.get("startedAt")),
            "finished_at": format_time(terminated.get("finishedAt")),
        }
    return None


def _statuses(pod: KubeObject) -> list[dict[str, Any]]:
    return list((pod.get("status") or {}).get("containerStatuses") or [])


def _state_reasons(statuses: list[dict[str, Any]]) -> list[str]:
    states = [
        detail
        for status in statuses
        for key in ("state", "lastState")
        for detail in (status.get(key) or {}).values()
    ]
    return status_words(states, "reason")


def _pod_identifiers(pod: KubeObject) -> tuple[str, ...]:
    meta = metadata(pod)
    labels = meta.get("labels") or {}
    owner_ref = owner(pod) or {}
    statuses = _statuses(pod)
    return collect_identifiers(
        [meta.get("name"), meta.get("namespace"), owner_ref.get("name")],
        [status.get("name") for status in statuses],
        labels.values(),
        status_words((pod.get("status") or {}).get("conditions") or [], "type", "reason"),
        _state_reasons(statuses),
    )


class ListPodsArgs(ToolArgs):
    """Arguments of ``k8s_list_pods``."""

    namespace: Namespace
    label_selector: LabelSelector | None = None


@tool(
    name="k8s_list_pods",
    kind="evidence",
    domain="kubernetes",
    source="kubernetes",
    read_only=True,
    timeout_s=_TIMEOUT_S,
    args=ListPodsArgs,
    identity_args=("namespace", "label_selector"),
)
async def k8s_list_pods(args: ListPodsArgs, backends: ToolBackends) -> ToolOutput:
    """List the pods of a namespace with phase, readiness, owner and per-container restarts.

    Optionally filtered by an equality-based label selector.
    """
    pods = sorted(
        await backends.kubernetes.list_pods(args.namespace, args.label_selector),
        key=lambda pod: str(metadata(pod).get("name")),
    )
    rows = []
    for pod in pods:
        statuses = _statuses(pod)
        rows.append(
            {
                "name": metadata(pod).get("name"),
                "phase": (pod.get("status") or {}).get("phase"),
                "ready": f"{sum(1 for s in statuses if s.get('ready'))}/{len(statuses)}",
                "owner": owner(pod),
                "restarts": sum(int(s.get("restartCount") or 0) for s in statuses),
                "containers": [
                    {
                        "name": s.get("name"),
                        "ready": bool(s.get("ready")),
                        "restart_count": int(s.get("restartCount") or 0),
                        "state": container_state(s.get("state")),
                    }
                    for s in statuses
                ],
            }
        )
    data = {"namespace": args.namespace, "label_selector": args.label_selector, "pods": rows}
    identifiers = collect_identifiers(*(_pod_identifiers(pod) for pod in pods))
    return ToolOutput(render(data), identifiers)


class PodStatusArgs(ToolArgs):
    """Arguments of ``k8s_get_pod_status``."""

    namespace: Namespace
    pod: ObjectName = Field(description="Pod name.")


@tool(
    name="k8s_get_pod_status",
    kind="evidence",
    domain="kubernetes",
    source="kubernetes",
    read_only=True,
    timeout_s=_TIMEOUT_S,
    args=PodStatusArgs,
    identity_args=("namespace", "pod"),
)
async def k8s_get_pod_status(args: PodStatusArgs, backends: ToolBackends) -> ToolOutput:
    """Read one pod's status: conditions and, per container, image, restarts and states.

    Includes the reason, exit code and times of the last termination of each container.
    """
    pod = await backends.kubernetes.get_pod(args.namespace, args.pod)
    meta = metadata(pod)
    status = pod.get("status") or {}
    data = {
        "namespace": args.namespace,
        "name": meta.get("name"),
        "labels": dict(sorted((meta.get("labels") or {}).items())),
        "owner": owner(pod),
        "phase": status.get("phase"),
        "started_at": format_time(status.get("startTime")),
        "conditions": [
            {"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason")}
            for c in status.get("conditions") or []
        ],
        "containers": [
            {
                "name": s.get("name"),
                "image": s.get("image"),
                "ready": bool(s.get("ready")),
                "restart_count": int(s.get("restartCount") or 0),
                "state": container_state(s.get("state")),
                "last_state": container_state(s.get("lastState")),
            }
            for s in _statuses(pod)
        ],
    }
    images = [str(s["image"]) for s in _statuses(pod) if s.get("image")]
    return ToolOutput(render(data), collect_identifiers(_pod_identifiers(pod), images))


class ListEventsArgs(ToolArgs):
    """Arguments of ``k8s_list_events``."""

    namespace: Namespace
    involved_object: ObjectName | None = Field(
        default=None, description="Only events about the object with this name."
    )
    since_minutes: int = Field(
        default=60, ge=1, le=1440, description="Only events from the last this many minutes."
    )


def _event_time(event: KubeObject) -> Any:
    return (
        event.get("lastTimestamp")
        or event.get("eventTime")
        or event.get("firstTimestamp")
        or metadata(event).get("creationTimestamp")
    )


@tool(
    name="k8s_list_events",
    kind="evidence",
    domain="kubernetes",
    source="kubernetes",
    read_only=True,
    timeout_s=_TIMEOUT_S,
    args=ListEventsArgs,
    identity_args=("namespace", "involved_object"),
)
async def k8s_list_events(args: ListEventsArgs, backends: ToolBackends) -> ToolOutput:
    """List recent events of a namespace, oldest first, optionally about one object.

    Each event has its type, reason, object, message, count and first and last times;
    at most the 50 newest are returned.
    """
    since = backends.now() - timedelta(minutes=args.since_minutes)
    selected = []
    for event in await backends.kubernetes.list_events(args.namespace):
        involved = event.get("involvedObject") or {}
        if args.involved_object and involved.get("name") != args.involved_object:
            continue
        when = parse_time(_event_time(event))
        if when is None or when < since:
            continue
        selected.append((when, str(metadata(event).get("name")), event))
    selected.sort(key=lambda item: (item[0], item[1]))
    kept = selected[-MAX_EVENTS:]
    rows = []
    names = []
    for when, _, event in kept:
        involved = event.get("involvedObject") or {}
        names += [involved.get("name"), involved.get("kind"), event.get("reason")]
        rows.append(
            {
                "time": format_time(when),
                "type": event.get("type"),
                "reason": event.get("reason"),
                "object": {"kind": involved.get("kind"), "name": involved.get("name")},
                "message": event.get("message"),
                "count": event.get("count") or 1,
                "first_time": format_time(event.get("firstTimestamp") or _event_time(event)),
            }
        )
    data = {
        "namespace": args.namespace,
        "involved_object": args.involved_object,
        "since": format_time(since),
        "matched": len(selected),
        "events": rows,
    }
    return ToolOutput(render(data), collect_identifiers([args.namespace], names))


class DeploymentArgs(ToolArgs):
    """Arguments of ``k8s_get_deployment``."""

    namespace: Namespace
    name: ObjectName = Field(description="Deployment name.")


def _env(entry: dict[str, Any]) -> dict[str, Any]:
    if "valueFrom" in entry:
        source = entry["valueFrom"] or {}
        kind = next(iter(source), "unknown")
        ref = source.get(kind) or {}
        reference = "/".join(str(ref[k]) for k in ("name", "key", "fieldPath") if k in ref)
        return {"name": entry.get("name"), "value_from": f"{kind} {reference}".strip()}
    return {"name": entry.get("name"), "value": entry.get("value", "")}


@tool(
    name="k8s_get_deployment",
    kind="evidence",
    domain="kubernetes",
    source="kubernetes",
    read_only=True,
    timeout_s=_TIMEOUT_S,
    args=DeploymentArgs,
    identity_args=("namespace", "name"),
)
async def k8s_get_deployment(args: DeploymentArgs, backends: ToolBackends) -> ToolOutput:
    """Read a deployment's spec and status: replicas, revision, conditions and containers.

    Per container: image, environment variables with their current values (a value
    from a Secret or ConfigMap is shown as a reference only), resource requests and
    limits, liveness and readiness probes.
    """
    deployment = await backends.kubernetes.get_deployment(args.namespace, args.name)
    meta = metadata(deployment)
    spec = deployment.get("spec") or {}
    status = deployment.get("status") or {}
    containers = ((spec.get("template") or {}).get("spec") or {}).get("containers") or []
    data = {
        "namespace": args.namespace,
        "name": meta.get("name"),
        "revision": (meta.get("annotations") or {}).get("deployment.kubernetes.io/revision"),
        "replicas": {
            "desired": spec.get("replicas"),
            "updated": status.get("updatedReplicas", 0),
            "ready": status.get("readyReplicas", 0),
            "available": status.get("availableReplicas", 0),
            "unavailable": status.get("unavailableReplicas", 0),
        },
        "selector": (spec.get("selector") or {}).get("matchLabels"),
        "strategy": (spec.get("strategy") or {}).get("type"),
        "conditions": [
            {
                "type": c.get("type"),
                "status": c.get("status"),
                "reason": c.get("reason"),
                "message": c.get("message"),
            }
            for c in status.get("conditions") or []
        ],
        "containers": [
            {
                "name": c.get("name"),
                "image": c.get("image"),
                "env": [_env(entry) for entry in c.get("env") or []],
                "resources": c.get("resources") or {},
                "liveness_probe": c.get("livenessProbe"),
                "readiness_probe": c.get("readinessProbe"),
            }
            for c in containers
        ],
    }
    identifiers = collect_identifiers(
        [meta.get("name"), args.namespace],
        [c.get("name") for c in containers],
        [c.get("image") for c in containers],
        status_words(status.get("conditions") or [], "type", "reason"),
    )
    return ToolOutput(render(data), identifiers)
