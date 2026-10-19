"""The replay backend: answers tool queries from an evidence pack.

A file the pack does not contain was not recorded, and reading it fails with
``not_found`` so that missing evidence is reported as missing rather than as empty.
Within a recorded file, queries behave like the API: a missing object is ``not_found``,
a list with no match is empty, and another namespace holds no objects. The current time
is the recording time, so time windows select the same events as during recording.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from bianque.tools.backends.base import KubeObject, ToolBackends
from bianque.tools.backends.git import GitCli, MissingRepository
from bianque.tools.errors import ToolError
from bianque.tools.pack.format import (
    K8S_DIR,
    K8S_FILES,
    REPO_FILE,
    PackError,
    PackMeta,
    load_faults,
    load_meta,
    log_path,
    read_json,
)
from bianque.tools.pack.repo import import_repository

_SELECTOR_TERM = re.compile(r"^(?P<key>[^=!,\s]+)\s*(?P<op>==|=|!=)\s*(?P<value>[^=!,\s]*)$")


def matches_selector(labels: dict[str, str], selector: str | None) -> bool:
    """Return whether labels satisfy an equality-based label selector."""
    if not selector:
        return True
    for term in selector.split(","):
        match = _SELECTOR_TERM.match(term.strip())
        if match is None:
            raise ToolError("invalid_arguments", f"label selector {selector}")
        key, op, value = match["key"], match["op"], match["value"]
        if op == "!=":
            if labels.get(key) == value:
                return False
        elif labels.get(key) != value:
            return False
    return True


def _name(obj: KubeObject) -> str:
    return str(obj.get("metadata", {}).get("name", ""))


class PackKubernetes:
    """Kubernetes objects and logs from a pack."""

    def __init__(self, pack: Path, namespace: str) -> None:
        """Read from ``pack``, which recorded ``namespace``."""
        self._pack = pack
        self._namespace = namespace
        self._cache: dict[str, list[KubeObject]] = {}

    def _objects(self, kind: str, namespace: str) -> list[KubeObject]:
        path = self._pack / K8S_DIR / K8S_FILES[kind]
        if not path.is_file():
            raise ToolError("not_found", f"{kind} {namespace}", f"{path.name} not recorded")
        if namespace != self._namespace:
            return []
        if kind not in self._cache:
            data: Any = read_json(path)
            if not isinstance(data, list):
                raise PackError(f"{path}: expected a JSON array")
            self._cache[kind] = data
        return self._cache[kind]

    def _one(self, kind: str, label: str, namespace: str, name: str) -> KubeObject:
        for obj in self._objects(kind, namespace):
            if _name(obj) == name:
                return obj
        raise ToolError("not_found", f"{label} {namespace}/{name}")

    async def list_pods(self, namespace: str, label_selector: str | None) -> list[KubeObject]:
        """Return the recorded pods matching the selector."""
        return [
            pod
            for pod in self._objects("pods", namespace)
            if matches_selector(pod.get("metadata", {}).get("labels") or {}, label_selector)
        ]

    async def get_pod(self, namespace: str, name: str) -> KubeObject:
        """Return one recorded pod."""
        return self._one("pods", "pod", namespace, name)

    async def list_events(self, namespace: str) -> list[KubeObject]:
        """Return the recorded events."""
        return list(self._objects("events", namespace))

    async def get_deployment(self, namespace: str, name: str) -> KubeObject:
        """Return one recorded deployment."""
        return self._one("deployments", "deployment", namespace, name)

    async def list_deployments(self, namespace: str) -> list[KubeObject]:
        """Return the recorded deployments."""
        return list(self._objects("deployments", namespace))

    async def list_replicasets(self, namespace: str) -> list[KubeObject]:
        """Return the recorded replica sets."""
        return list(self._objects("replicasets", namespace))

    async def read_log(self, namespace: str, pod: str, container: str, previous: bool) -> str:
        """Return a recorded container log."""
        target = f"log {namespace}/{pod}/{container}" + (" previous" if previous else "")
        path = log_path(self._pack, pod, container, previous)
        if namespace != self._namespace or not path.is_file():
            raise ToolError("not_found", target)
        return path.read_text()


@asynccontextmanager
async def open_pack(pack: Path) -> AsyncIterator[ToolBackends]:
    """Open an evidence pack as tool backends.

    The manifest repository is imported into a temporary directory that is removed
    when the context exits.

    Raises:
        PackError: The pack cannot be loaded.
    """
    from bianque.tools.registry import registry

    meta: PackMeta = load_meta(pack)
    faults = load_faults(pack, {item.name for item in registry})
    workdir = Path(tempfile.mkdtemp(prefix="bianque-pack-"))
    try:
        repo_file = pack / REPO_FILE
        git: GitCli | MissingRepository
        if repo_file.is_file():
            repo = workdir / "repo.git"
            await import_repository(repo_file.read_text(), meta.repo_head, repo)
            git = GitCli(repo)
        else:
            git = MissingRepository()
        yield ToolBackends(
            kind="pack",
            kubernetes=PackKubernetes(pack, meta.namespace),
            git=git,
            now=lambda: meta.recorded_at,
            faults=faults,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
