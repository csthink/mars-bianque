"""Recording an evidence pack from the demo environment.

Recording takes a snapshot of one namespace (pods, events, deployments, replica sets
and every container log) and of the manifest repository, plus the alert that started
the case. It runs in the demo profile, outside the tool executor and without a
database.

Before anything is written:

- ``metadata.managedFields`` and the ``last-applied-configuration`` annotation are
  dropped from every object. They record which client wrote which field and a copy of
  the manifest; no tool reads them.
- Private addresses are generalized the way the redaction engine does it: the first
  two octets stay, the last two become ``x.x``. Pod and node addresses of a kind
  cluster carry no diagnostic value, and the pack is published.

After writing, every file is checked with the redaction engine's ``detect()``: a rule
hit (a credential shape) fails the recording. High-entropy hits are only counted,
because Kubernetes UIDs and commit hashes are random strings by design.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from bianque.redaction import detect, generalize_private_ips
from bianque.tools.backends.base import KubeObject, KubernetesSource
from bianque.tools.errors import ToolError
from bianque.tools.pack.format import (
    ALERT_FILE,
    K8S_DIR,
    K8S_FILES,
    META_FILE,
    REPO_FILE,
    PackError,
    PackMeta,
    log_path,
)
from bianque.tools.pack.repo import export_repository

_LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


@dataclass(frozen=True)
class RecordSummary:
    """What a recording wrote."""

    files: int
    objects: dict[str, int]
    logs: int
    high_entropy_hits: int


def clean_object(obj: KubeObject) -> KubeObject:
    """Return a copy without managed fields and the last-applied annotation."""
    copy: KubeObject = json.loads(json.dumps(obj))
    meta = copy.get("metadata")
    if isinstance(meta, dict):
        meta.pop("managedFields", None)
        annotations = meta.get("annotations")
        if isinstance(annotations, dict):
            annotations.pop(_LAST_APPLIED, None)
            if not annotations:
                meta.pop("annotations")
    return copy


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generalize_private_ips(text))


def _write_json(path: Path, data: Any) -> None:
    _write_text(path, json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def prepare_output(out: Path) -> None:
    """Make ``out`` an empty directory, replacing an earlier pack but nothing else.

    Raises:
        PackError: ``out`` exists, is not empty and is not an evidence pack.
    """
    if out.exists():
        if any(out.iterdir()) and not (out / META_FILE).is_file():
            raise PackError(f"{out}: exists and is not an evidence pack; refusing to replace it")
        shutil.rmtree(out)
    out.mkdir(parents=True)


def scan_pack(pack: Path) -> int:
    """Check every file of a pack for credential shapes.

    Returns:
        The number of high-entropy hits, which are expected and only reported.

    Raises:
        PackError: A redaction rule matched; names each file and rule.
    """
    problems: list[str] = []
    entropy = 0
    for path in sorted(p for p in pack.rglob("*") if p.is_file()):
        rules = Counter(finding.rule for finding in detect(path.read_text()))
        entropy += rules.pop("high-entropy", 0)
        problems += [f"{path.relative_to(pack)}: {rule} x{count}" for rule, count in rules.items()]
    if problems:
        raise PackError("redaction rules matched in the pack: " + "; ".join(problems))
    return entropy


async def record_pack(
    kubernetes: KubernetesSource,
    *,
    namespace: str,
    alert_text: str,
    repo: Path | None,
    case: str,
    out: Path,
    recorded_at: datetime,
    versions: Mapping[str, str],
) -> RecordSummary:
    """Record an evidence pack.

    Args:
        kubernetes: The real Kubernetes backend.
        namespace: The namespace to snapshot.
        alert_text: The Alertmanager webhook body that started the case.
        repo: The manifest repository; ``None`` records no repository.
        case: The case name written to ``meta.json``.
        out: The pack directory; an earlier pack there is replaced.
        recorded_at: The recording time, the "current time" of replay.
        versions: Component versions for ``meta.json``.

    Returns:
        A summary of what was written.

    Raises:
        PackError: The alert is not a JSON object, the output directory cannot be
            used, or a redaction rule matched in the written files.
    """
    try:
        alert = json.loads(alert_text)
    except ValueError as exc:
        raise PackError(f"the alert is not JSON: {exc}") from exc
    if not isinstance(alert, dict):
        raise PackError("the alert must be a JSON object (an Alertmanager webhook body)")
    prepare_output(out)
    _write_json(out / ALERT_FILE, alert)

    objects = {
        "pods": await kubernetes.list_pods(namespace, None),
        "events": await kubernetes.list_events(namespace),
        "deployments": await kubernetes.list_deployments(namespace),
        "replicasets": await kubernetes.list_replicasets(namespace),
    }
    for kind, items in objects.items():
        cleaned = sorted(
            (clean_object(item) for item in items),
            key=lambda item: str((item.get("metadata") or {}).get("name")),
        )
        _write_json(out / K8S_DIR / K8S_FILES[kind], cleaned)

    logs = 0
    for pod in objects["pods"]:
        name = str((pod.get("metadata") or {}).get("name"))
        containers = [c.get("name") for c in (pod.get("spec") or {}).get("containers") or []]
        for container in containers:
            for previous in (False, True):
                try:
                    text = await kubernetes.read_log(namespace, name, str(container), previous)
                except ToolError as exc:
                    if exc.category == "not_found":
                        continue
                    raise
                _write_text(log_path(out, name, str(container), previous), text)
                logs += 1

    head: str | None = None
    if repo is not None:
        text, head = await export_repository(repo)
        _write_text(out / REPO_FILE, text)

    meta = PackMeta(
        case=case,
        namespace=namespace,
        recorded_at=recorded_at,
        repo_head=head,
        versions=dict(versions),
    )
    _write_json(out / META_FILE, meta.to_json())
    entropy = scan_pack(out)
    return RecordSummary(
        files=sum(1 for p in out.rglob("*") if p.is_file()),
        objects={kind: len(items) for kind, items in objects.items()},
        logs=logs,
        high_entropy_hits=entropy,
    )
