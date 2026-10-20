"""The evidence domains: the only place that names them.

The first-round range check of the plan, the grouping of steps by sub-agent, the
default plan, the sufficiency rule of the assessment, the tool selection of each
sub-agent and the confidence limit after a failed call all read this declaration.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StepTemplate:
    """A step of the default plan."""

    goal: str
    suggested_tools: tuple[str, ...]
    expected_evidence: str


@dataclass(frozen=True)
class EvidenceDomain:
    """One evidence domain.

    Attributes:
        name: The domain name; the evidence tools of the registry declare it.
        first_round: Whether the first plan may have steps of this domain. Change
            attribution comes after the current state is known.
        default_steps: The steps of the default plan for this domain.
        required_for_categories: Root cause categories that need evidence of this domain
            before the assessment may call the evidence sufficient.
        failure_caps_confidence: Whether a failed call of this domain rules out a
            ``high`` root cause confidence.
    """

    name: str
    first_round: bool
    default_steps: tuple[StepTemplate, ...]
    required_for_categories: frozenset[str] = frozenset()
    failure_caps_confidence: bool = False


DOMAINS: tuple[EvidenceDomain, ...] = (
    EvidenceDomain(
        name="kubernetes",
        first_round=True,
        default_steps=(
            StepTemplate(
                "List the pods named in the alert with their phase and restart counts.",
                ("k8s_list_pods",),
                "The pods of the alerted workload, their phase and restart counts.",
            ),
            StepTemplate(
                "Read the status of the restarting pod.",
                ("k8s_get_pod_status",),
                "Container states, with the reason and exit code of the last termination.",
            ),
            StepTemplate(
                "List the recent events about the restarting pod.",
                ("k8s_list_events",),
                "Events such as back-off, probe failures or scheduling problems.",
            ),
            StepTemplate(
                "Read the deployment that owns the restarting pod.",
                ("k8s_get_deployment",),
                "Image, environment variables, resource limits and probes of the containers.",
            ),
        ),
    ),
    EvidenceDomain(
        name="logs",
        first_round=True,
        default_steps=(
            StepTemplate(
                "Read the logs of the previous container instance of the restarting pod.",
                ("logs_get_container_logs",),
                "The last log lines before the container exited.",
            ),
            StepTemplate(
                "Read the logs of the current container instance of the restarting pod.",
                ("logs_get_container_logs",),
                "The log lines of the running or starting container.",
            ),
        ),
        failure_caps_confidence=True,
    ),
    EvidenceDomain(
        name="change",
        first_round=False,
        default_steps=(
            StepTemplate(
                "Read the rollout history of the deployment.",
                ("change_get_rollout_history",),
                "Revisions and the pod template difference between the last two.",
            ),
            StepTemplate(
                "List the recent commits of the manifest repository.",
                ("change_list_commits",),
                "Commits with author, time, message and changed files.",
            ),
            StepTemplate(
                "Show the most recent commit that changed the deployment manifest.",
                ("change_show_commit",),
                "The diff of that commit.",
            ),
        ),
        required_for_categories=frozenset(
            {"config_error", "resource_limit", "probe_misconfig", "image_error"}
        ),
    ),
)


#: Tools whose evidence is the pod status. Without it the assessment never calls the
#: evidence sufficient: the state of the restarting container is what everything else
#: is checked against.
POD_STATUS_TOOLS: tuple[str, ...] = ("k8s_get_pod_status",)


def domain(name: str) -> EvidenceDomain:
    """Return the declaration of one domain.

    Raises:
        KeyError: When no domain has this name.
    """
    for item in DOMAINS:
        if item.name == name:
            return item
    raise KeyError(name)
