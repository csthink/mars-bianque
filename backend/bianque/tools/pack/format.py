"""The evidence pack format.

An evidence pack is a snapshot of the demo namespace and the manifest repository taken
when a fault was live, not a recording of individual calls, so the replay backend can
answer any query the tools make, in any order::

    meta.json          schema_version, case, namespace, recording time, versions
    alert.json         the Alertmanager webhook body
    k8s/               pods.json, events.json, deployments.json, replicasets.json
    logs/              <pod>/<container>.log, <pod>/<container>.previous.log
    repo.fast-export   the manifest repository as git fast-export text
    faults.yaml        optional faults injected during replay

A file that is absent was not recorded; tools reading it fail with ``not_found``.
Loading a pack whose schema version differs from ``SCHEMA_VERSION`` fails and names
the file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, get_args

import yaml

from bianque.tools.backends.base import FaultMode

#: The pack layout version this code reads and writes.
SCHEMA_VERSION = 1

META_FILE = "meta.json"
ALERT_FILE = "alert.json"
K8S_DIR = "k8s"
LOGS_DIR = "logs"
REPO_FILE = "repo.fast-export"
FAULTS_FILE = "faults.yaml"

#: Kubernetes object lists by kind, as file names under ``k8s/``.
K8S_FILES = {
    "pods": "pods.json",
    "events": "events.json",
    "deployments": "deployments.json",
    "replicasets": "replicasets.json",
}


class PackError(Exception):
    """An evidence pack cannot be loaded or written."""


@dataclass(frozen=True)
class PackMeta:
    """The contents of ``meta.json``."""

    case: str
    namespace: str
    recorded_at: datetime
    repo_head: str | None = None
    source_case: str | None = None
    versions: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        """Return the JSON form, including the schema version."""
        data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "case": self.case,
            "namespace": self.namespace,
            "recorded_at": self.recorded_at.isoformat(),
            "versions": dict(self.versions),
        }
        if self.repo_head is not None:
            data["repo_head"] = self.repo_head
        if self.source_case is not None:
            data["source_case"] = self.source_case
        return data


def write_json(path: Path, data: Any) -> None:
    """Write JSON with sorted keys and two-space indentation, ending with a newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def read_json(path: Path) -> Any:
    """Read a JSON file of a pack.

    Raises:
        PackError: The file is not valid JSON.
    """
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise PackError(f"{path}: cannot read JSON: {exc}") from exc


def log_path(pack: Path, pod: str, container: str, previous: bool) -> Path:
    """Return where a container log is stored in a pack."""
    suffix = ".previous.log" if previous else ".log"
    return pack / LOGS_DIR / pod / f"{container}{suffix}"


def load_meta(pack: Path) -> PackMeta:
    """Load and check ``meta.json``.

    Raises:
        PackError: The file is missing, malformed, or has another schema version.
    """
    path = pack / META_FILE
    if not path.is_file():
        raise PackError(f"{path}: not an evidence pack (no {META_FILE})")
    data = read_json(path)
    if not isinstance(data, dict):
        raise PackError(f"{path}: expected a JSON object")
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise PackError(
            f"{path}: schema_version {version!r} is not supported (expected {SCHEMA_VERSION})"
        )
    try:
        return PackMeta(
            case=str(data["case"]),
            namespace=str(data["namespace"]),
            recorded_at=datetime.fromisoformat(str(data["recorded_at"])),
            repo_head=data.get("repo_head"),
            source_case=data.get("source_case"),
            versions={str(k): str(v) for k, v in (data.get("versions") or {}).items()},
        )
    except (KeyError, ValueError) as exc:
        raise PackError(f"{path}: invalid field: {exc}") from exc


def load_faults(pack: Path, known_tools: set[str]) -> dict[str, FaultMode]:
    """Load ``faults.yaml``; an absent file means no faults.

    Raises:
        PackError: The file is malformed, names an unknown tool or an unknown mode.
    """
    path = pack / FAULTS_FILE
    if not path.exists():
        return {}
    try:
        entries = yaml.safe_load(path.read_text()) or []
    except yaml.YAMLError as exc:
        raise PackError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(entries, list):
        raise PackError(f"{path}: expected a list of {{tool, mode}} entries")
    modes = set(get_args(FaultMode))
    faults: dict[str, FaultMode] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"tool", "mode"}:
            raise PackError(f"{path}: each entry needs exactly the keys tool and mode")
        tool, mode = entry["tool"], entry["mode"]
        if tool not in known_tools:
            raise PackError(f"{path}: unknown tool {tool!r}")
        if mode not in modes:
            raise PackError(f"{path}: unknown mode {mode!r} (expected one of {sorted(modes)})")
        faults[tool] = mode
    return faults


def write_faults(pack: Path, faults: dict[str, FaultMode]) -> None:
    """Write ``faults.yaml`` with entries in tool name order."""
    entries = [{"tool": tool, "mode": faults[tool]} for tool in sorted(faults)]
    (pack / FAULTS_FILE).write_text(yaml.safe_dump(entries, sort_keys=True))
