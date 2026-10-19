"""Test doubles for the tool executor: an in-memory sink and backends without I/O."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from bianque.tools.backends.base import FaultMode, GitSubcommand, KubeObject, ToolBackends
from bianque.tools.errors import ToolError
from bianque.tools.store import EvidenceRecord, ToolCallRecord

#: Root of the evaluation cases, where the committed evidence packs live.
CASES = Path(__file__).resolve().parents[3] / "eval" / "cases"

NOW = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


@dataclass
class MemorySink:
    """Keeps every call record and evidence in lists."""

    calls: list[ToolCallRecord] = field(default_factory=list)
    evidence: list[EvidenceRecord] = field(default_factory=list)

    async def save(self, call: ToolCallRecord, evidence: EvidenceRecord | None) -> None:
        self.calls.append(call)
        if evidence is not None:
            self.evidence.append(evidence)


class NoKubernetes:
    """A Kubernetes source that fails every call; for tools that do not use it."""

    def __getattr__(self, name: str) -> object:
        async def fail(*args: object, **kwargs: object) -> object:
            raise AssertionError(f"unexpected Kubernetes call {name}")

        return fail


class NoGit:
    """A git source that fails every call."""

    async def run(self, subcommand: GitSubcommand, args: Sequence[str]) -> str:
        raise AssertionError(f"unexpected git {subcommand}")


@dataclass
class FakeKubernetes:
    """Answers from in-memory objects, like an evidence pack would."""

    pods: list[KubeObject] = field(default_factory=list)
    events: list[KubeObject] = field(default_factory=list)
    deployments: list[KubeObject] = field(default_factory=list)
    replicasets: list[KubeObject] = field(default_factory=list)
    logs: dict[tuple[str, str, bool], str] = field(default_factory=dict)

    async def list_pods(self, namespace: str, label_selector: str | None) -> list[KubeObject]:
        return list(self.pods)

    async def get_pod(self, namespace: str, name: str) -> KubeObject:
        for pod in self.pods:
            if pod["metadata"]["name"] == name:
                return pod
        raise ToolError("not_found", f"pod {namespace}/{name}")

    async def list_events(self, namespace: str) -> list[KubeObject]:
        return list(self.events)

    async def get_deployment(self, namespace: str, name: str) -> KubeObject:
        raise ToolError("not_found", f"deployment {namespace}/{name}")

    async def list_deployments(self, namespace: str) -> list[KubeObject]:
        return list(self.deployments)

    async def list_replicasets(self, namespace: str) -> list[KubeObject]:
        return list(self.replicasets)

    async def read_log(self, namespace: str, pod: str, container: str, previous: bool) -> str:
        try:
            return self.logs[(pod, container, previous)]
        except KeyError:
            raise ToolError("not_found", f"log {namespace}/{pod}/{container}") from None


def backends(
    kubernetes: object | None = None,
    *,
    faults: dict[str, FaultMode] | None = None,
) -> ToolBackends:
    """Backends for unit tests; the clock is fixed at ``NOW``."""
    return ToolBackends(
        kind="pack",
        kubernetes=kubernetes or NoKubernetes(),  # type: ignore[arg-type]
        git=NoGit(),
        now=lambda: NOW,
        faults=faults or {},
    )


async def no_sleep(delay: float) -> None:
    """Retry delay that does not wait; records nothing."""
    return None
