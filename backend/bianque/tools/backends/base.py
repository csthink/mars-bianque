"""Interfaces between the tools and their data sources.

Every evidence tool has a real backend (the cluster and the manifest repository) and a
replay backend (an evidence pack) behind the same interface. Backends only fetch raw
data; filtering, sorting, truncation and rendering happen in the tool functions, so the
two backends produce the same tool output for the same query.

Kubernetes objects are plain dicts in the API's JSON form, with the same field names
as ``kubectl get -o json``: the real backend serializes client objects into that form,
and an evidence pack stores it as recorded.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from bianque.tools.errors import ToolError

#: A Kubernetes object in the API's JSON form.
KubeObject = dict[str, Any]

#: The git subcommands a tool may run. Anything else is refused at runtime.
GitSubcommand = Literal["log", "show", "rev-parse"]

#: Fault modes an evidence pack can inject.
FaultMode = Literal["timeout"]


class KubernetesSource(Protocol):
    """Read-only access to one cluster's objects and logs."""

    async def list_pods(self, namespace: str, label_selector: str | None) -> list[KubeObject]:
        """Return the pods of a namespace, optionally filtered by an equality selector."""
        ...

    async def get_pod(self, namespace: str, name: str) -> KubeObject:
        """Return one pod; ``not_found`` when it does not exist."""
        ...

    async def list_events(self, namespace: str) -> list[KubeObject]:
        """Return all events of a namespace."""
        ...

    async def get_deployment(self, namespace: str, name: str) -> KubeObject:
        """Return one deployment; ``not_found`` when it does not exist."""
        ...

    async def list_deployments(self, namespace: str) -> list[KubeObject]:
        """Return all deployments of a namespace."""
        ...

    async def list_replicasets(self, namespace: str) -> list[KubeObject]:
        """Return all replica sets of a namespace."""
        ...

    async def read_log(self, namespace: str, pod: str, container: str, previous: bool) -> str:
        """Return a container's log; ``not_found`` when there is none."""
        ...


class GitSource(Protocol):
    """Read-only access to the manifest repository."""

    async def run(self, subcommand: GitSubcommand, args: Sequence[str]) -> str:
        """Run one allowed git subcommand and return its standard output."""
        ...


@dataclass
class ToolBackends:
    """The data sources of one run, as the tool functions see them.

    Attributes:
        kind: ``live`` or ``pack``; written into evidence metadata only. Tool functions
            must not read it.
        kubernetes: Cluster objects and logs.
        git: The manifest repository.
        now: The current time for time windows: the wall clock for the real backend,
            the recording time for an evidence pack.
        faults: Tool name to fault mode, from an evidence pack's ``faults.yaml``.
    """

    kind: Literal["live", "pack"]
    kubernetes: KubernetesSource
    git: GitSource
    now: Callable[[], datetime]
    faults: Mapping[str, FaultMode] = field(default_factory=dict)

    def injected_fault(self, tool: str) -> ToolError | None:
        """Return the error an evidence pack injects for ``tool``, if any."""
        mode = self.faults.get(tool)
        if mode == "timeout":
            return ToolError("timeout", tool, "injected by the evidence pack")
        return None
