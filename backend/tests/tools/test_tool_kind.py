"""Integration tests against the demo kind cluster while an S1 fault is injected.

They run only when ``BIANQUE_TEST_KIND_CASE`` names the injected case (``a``, ``b``,
``c`` or ``e``); ``demo/scripts/record-pack.sh`` sets it right after recording the
case's evidence pack, whose alert names the failing pod. Other variables:

- ``BIANQUE_TEST_KIND_CONTEXT``: kubeconfig context of the demo cluster
  (default ``kind-bianque-demo``); the host's current context is never used.
- ``BIANQUE_TEST_KIND_REPO``: the manifest repository (default ``../.demo/manifests``).

Two things are checked:

1. The real backends deliver the case's required evidence, with the same checks the
   evidence pack tests apply to the replay backend (``s1_evidence``).
2. The same query gives the same output from the real backends and from an evidence
   pack recorded at that moment, once the recording's private address generalization
   is applied to the real output; the real backends take the recording time as the
   current time, as replay does. The cluster keeps changing (restart counts, event
   counts), so each round reads the real output, records a pack, reads the real output
   again, and compares only the queries whose two real readings agree. A query that
   changed is compared in the next round, up to three rounds; every query must be
   compared.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from s1_evidence import DEPLOYMENT, NAMESPACE, check_required_evidence
from tool_fakes import CASES

from bianque.config import Settings
from bianque.redaction import generalize_private_ips
from bianque.tools import registry
from bianque.tools.backends import open_live, open_pack
from bianque.tools.backends.base import ToolBackends
from bianque.tools.errors import ToolError
from bianque.tools.pack.record import record_pack

CASE = os.environ.get("BIANQUE_TEST_KIND_CASE", "")
CONTEXT = os.environ.get("BIANQUE_TEST_KIND_CONTEXT", "kind-bianque-demo")
REPO = Path(os.environ.get("BIANQUE_TEST_KIND_REPO", CASES.parents[1] / ".demo" / "manifests"))
PACK = CASES / f"s1-{CASE}" / "pack"
ROUNDS = 3

pytestmark = pytest.mark.skipif(
    CASE not in ("a", "b", "c", "e"), reason="set BIANQUE_TEST_KIND_CASE to the injected case"
)


def _settings() -> Settings:
    return Settings(tool_backend="live", kube_context=CONTEXT, git_repo_path=REPO)


def _alert() -> dict[str, Any]:
    alert: dict[str, Any] = json.loads((PACK / "alert.json").read_text())
    return alert


async def test_live_backends_deliver_the_required_evidence() -> None:
    async with open_live(_settings()) as live:
        await check_required_evidence(live, CASE, _alert())


async def _raw(backends: ToolBackends, name: str, args: dict[str, Any]) -> tuple[str, str]:
    definition = registry.get(name)
    assert definition is not None
    try:
        output = await definition.function(definition.args_model.model_validate(args), backends)
    except ToolError as exc:
        return "error", exc.model_text
    return "ok", output.text


async def _queries(live: ToolBackends) -> list[tuple[str, dict[str, Any]]]:
    labels = _alert()["alerts"][0]["labels"]
    pod, container = labels["pod"], labels["container"]
    queries: list[tuple[str, dict[str, Any]]] = [
        ("k8s_list_pods", {"namespace": NAMESPACE}),
        (
            "k8s_list_pods",
            {"namespace": NAMESPACE, "label_selector": "app.kubernetes.io/name=s1-app"},
        ),
        ("k8s_list_events", {"namespace": NAMESPACE}),
        ("k8s_list_events", {"namespace": NAMESPACE, "involved_object": pod}),
        ("k8s_get_deployment", {"namespace": NAMESPACE, "name": DEPLOYMENT}),
        ("k8s_get_deployment", {"namespace": NAMESPACE, "name": "missing"}),
        ("change_get_rollout_history", {"namespace": NAMESPACE, "deployment": DEPLOYMENT}),
        ("change_list_commits", {}),
        ("change_list_commits", {"path": "deployment.yaml"}),
    ]
    pods = [item["metadata"]["name"] for item in await live.kubernetes.list_pods(NAMESPACE, None)]
    assert pod in pods
    for name in pods:
        queries.append(("k8s_get_pod_status", {"namespace": NAMESPACE, "pod": name}))
        for previous in (False, True):
            args = {"namespace": NAMESPACE, "pod": name, "container": container}
            queries.append(("logs_get_container_logs", {**args, "previous": previous}))
    commits = json.loads((await _raw(live, "change_list_commits", {}))[1])["commits"]
    queries += [("change_show_commit", {"commit": c["commit"]}) for c in commits]
    return queries


def _clock(at: datetime) -> Callable[[], datetime]:
    return lambda: at


async def test_real_and_replay_backends_return_the_same_output(tmp_path: Path) -> None:
    alert_text = (PACK / "alert.json").read_text()
    async with open_live(_settings()) as connected:
        queries = await _queries(connected)
        pending = list(queries)
        compared = 0
        for round_no in range(1, ROUNDS + 1):
            recorded_at = datetime.now(UTC).replace(microsecond=0)
            live = replace(connected, now=_clock(recorded_at))
            first = [await _raw(live, *query) for query in pending]
            pack_dir = tmp_path / f"round-{round_no}"
            await record_pack(
                live.kubernetes,
                namespace=NAMESPACE,
                alert_text=alert_text,
                repo=REPO,
                case=f"s1-{CASE}",
                out=pack_dir,
                recorded_at=recorded_at,
                versions={},
            )
            async with open_pack(pack_dir) as pack:
                replayed = [await _raw(pack, *query) for query in pending]
            second = [await _raw(live, *query) for query in pending]
            unstable = []
            for query, before, after, from_pack in zip(
                pending, first, second, replayed, strict=True
            ):
                if before != after:
                    unstable.append(query)
                    continue
                expected = (before[0], generalize_private_ips(before[1]))
                assert from_pack == expected, (query, expected, from_pack)
                compared += 1
            pending = unstable
            if not pending:
                break
        assert not pending, f"queries never stable across {ROUNDS} rounds: {pending}"
        assert compared == len(queries)
