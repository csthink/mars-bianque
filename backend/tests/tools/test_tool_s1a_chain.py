"""The S1-a tool chain keeps the names each next step needs through redaction.

Every argument is read from the previous step's ``model_view``, the only text a model
sees. Without preserved identifiers the entropy fallback of the redaction would mask the
pod name and the commit hash, and the chain would break.
"""

from __future__ import annotations

import json

from s1_evidence import NAMESPACE, Tools, changed
from tool_fakes import CASES

from bianque.redaction import build_engine, redact
from bianque.tools import registry
from bianque.tools.backends.pack import open_pack
from bianque.tools.change import ShowCommitArgs


async def test_names_survive_from_step_to_step() -> None:
    async with open_pack(CASES / "s1-a" / "pack") as backends:
        tools = Tools(backends)
        pods = json.loads(
            (await tools.result("k8s_list_pods", namespace=NAMESPACE)).model_view.text
        )
        failing = next(row for row in pods["pods"] if row["restarts"] > 0)
        pod, container = failing["name"], failing["containers"][0]["name"]
        assert (
            pod
            == json.loads((CASES / "s1-a/pack/alert.json").read_text())["alerts"][0]["labels"][
                "pod"
            ]
        )

        status = await tools.data("k8s_get_pod_status", namespace=NAMESPACE, pod=pod)
        assert status["name"] == pod
        assert status["containers"][0]["last_state"]["exit_code"] == 2

        logs = await tools.data(
            "logs_get_container_logs",
            namespace=NAMESPACE,
            pod=pod,
            container=container,
            previous=True,
        )
        assert any("invalid APP_MODE" in line for line in logs["lines"])

        commits = await tools.data("change_list_commits")
        commit = commits["commits"][0]["commit"]
        shown_result = await tools.result("change_show_commit", commit=commit)
        shown = json.loads(shown_result.model_view.text)
        assert shown["short"] == commit
        assert changed(shown["diff"], "+", "value: invalid")
        full = shown["commit"]
        assert len(full) == 40 and full in shown_result.model_view.text

        # Control: the same result redacted without preserved identifiers loses the
        # full hash (and with it, any later reference to this commit).
        definition = registry.get("change_show_commit")
        assert definition is not None
        raw = await definition.function(ShowCommitArgs(commit=commit), backends)
        assert full in raw.text
        assert full not in redact(raw.text)
        assert redact(pod) != pod


async def test_container_images_survive_redaction() -> None:
    """An image reference stays readable, so an image pull failure can name the image."""
    async with open_pack(CASES / "s1-a" / "pack") as backends:
        tools = Tools(backends)
        alert = json.loads((CASES / "s1-a/pack/alert.json").read_text())["alerts"][0]
        status = await tools.result(
            "k8s_get_pod_status", namespace=NAMESPACE, pod=alert["labels"]["pod"]
        )
        image = json.loads(status.model_view.text)["containers"][0]["image"]
        assert image == "docker.io/library/bianque-s1-app:dev"
        assert image in status.identifiers
        assert redact(image) != image

        deployment = await tools.result("k8s_get_deployment", namespace=NAMESPACE, name="s1-app")
        spec_image = json.loads(deployment.model_view.text)["containers"][0]["image"]
        assert spec_image in deployment.identifiers


def test_preserved_images_still_pass_the_rule_based_detectors() -> None:
    """Preserving an image only exempts it from the entropy check, not from the rules."""
    engine = build_engine(("corp.internal",))
    image = "registry.corp.internal/team/app-7d9f8c6b5x2k4pqq:dev"
    assert "corp.internal" not in engine.redact(image, preserve=[image])
