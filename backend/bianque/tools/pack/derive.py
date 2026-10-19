"""Deriving an evidence pack from another one.

Some cases are not recorded from the cluster but derived from a recorded pack: one
injects a tool fault during replay, another withholds most of the evidence. A derived
pack is a complete, independent copy; it does not refer to its source.
"""

from __future__ import annotations

import shutil
from collections.abc import Collection, Mapping
from dataclasses import replace
from pathlib import Path

from bianque.tools.backends.base import FaultMode
from bianque.tools.pack.format import (
    ALERT_FILE,
    FAULTS_FILE,
    K8S_DIR,
    K8S_FILES,
    META_FILE,
    PackError,
    load_meta,
    write_faults,
    write_json,
)
from bianque.tools.pack.record import prepare_output

#: What ``only`` can keep, as paths inside a pack (``meta.json`` is always kept).
KEEPABLE = {
    "alert": ALERT_FILE,
    "pods": f"{K8S_DIR}/{K8S_FILES['pods']}",
    "events": f"{K8S_DIR}/{K8S_FILES['events']}",
    "deployments": f"{K8S_DIR}/{K8S_FILES['deployments']}",
    "replicasets": f"{K8S_DIR}/{K8S_FILES['replicasets']}",
}


def derive_pack(
    source: Path,
    out: Path,
    *,
    case: str,
    faults: Mapping[str, FaultMode] | None = None,
    only: Collection[str] | None = None,
    known_tools: Collection[str] = (),
) -> None:
    """Write a pack derived from ``source`` to ``out``.

    Args:
        source: The recorded pack.
        out: The derived pack directory; an earlier pack there is replaced.
        case: The case name of the derived pack.
        faults: Faults to inject during replay, by tool name.
        only: Keep only these parts (keys of ``KEEPABLE``); everything else is dropped.
        known_tools: Tool names a fault may name.

    Raises:
        PackError: The source cannot be loaded, a part or a fault is unknown.
    """
    meta = load_meta(source)
    if source.resolve() == out.resolve():
        raise PackError(f"{out}: the derived pack cannot replace its source")
    unknown_tools = sorted(set(faults or {}) - set(known_tools))
    if unknown_tools:
        raise PackError(f"faults name unknown tools {unknown_tools}")
    if only is not None and set(only) - set(KEEPABLE):
        unknown = sorted(set(only) - set(KEEPABLE))
        raise PackError(f"unknown parts {unknown}; expected some of {sorted(KEEPABLE)}")
    prepare_output(out)
    if only is None:
        shutil.copytree(source, out, dirs_exist_ok=True)
    else:
        for part in only:
            target = out / KEEPABLE[part]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / KEEPABLE[part], target)
    (out / FAULTS_FILE).unlink(missing_ok=True)
    if faults:
        write_faults(out, dict(faults))
    derived = replace(meta, case=case, source_case=meta.case)
    if only is not None:
        derived = replace(derived, repo_head=None)
    write_json(out / META_FILE, derived.to_json())
