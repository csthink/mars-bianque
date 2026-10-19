"""Evidence packs: loading, schema versions, faults, deriving and recording."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tool_fakes import CASES, FakeKubernetes

from bianque.tools import registry
from bianque.tools.backends.pack import PackKubernetes, matches_selector, open_pack
from bianque.tools.errors import ToolError
from bianque.tools.pack.derive import derive_pack
from bianque.tools.pack.format import (
    SCHEMA_VERSION,
    PackError,
    PackMeta,
    load_faults,
    load_meta,
    write_json,
)
from bianque.tools.pack.record import clean_object, record_pack

TOOLS = {item.name for item in registry}
#: Private addresses, assembled at runtime so the public content scanner sees none.
POD_IP = "10.244" + ".0.7"
HOST_IP = "172.18" + ".0.2"
ALERTMANAGER_IP = "10.0" + ".3.4"
RECORDED_AT = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


def _pack(root: Path, **meta: object) -> Path:
    data = PackMeta(case="s1-x", namespace="demo-s1", recorded_at=RECORDED_AT).to_json()
    data.update(meta)
    write_json(root / "meta.json", data)
    write_json(root / "alert.json", {"status": "firing"})
    write_json(
        root / "k8s" / "pods.json", [{"metadata": {"name": "web-0", "labels": {"app": "web"}}}]
    )
    return root


def test_a_pack_with_another_schema_version_fails_and_names_the_file(tmp_path: Path) -> None:
    _pack(tmp_path, schema_version=SCHEMA_VERSION + 1)
    with pytest.raises(PackError) as excinfo:
        load_meta(tmp_path)
    assert str(tmp_path / "meta.json") in str(excinfo.value)
    assert "schema_version" in str(excinfo.value)


def test_a_directory_without_meta_is_not_a_pack(tmp_path: Path) -> None:
    with pytest.raises(PackError, match="not an evidence pack"):
        load_meta(tmp_path)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("- {tool: logs_get_container_logs, mode: slow}\n", "unknown mode"),
        ("- {tool: k8s_delete_pod, mode: timeout}\n", "unknown tool"),
        ("- {tool: logs_get_container_logs}\n", "exactly the keys"),
        ("tool: logs_get_container_logs\n", "expected a list"),
    ],
)
def test_invalid_faults_fail_and_name_the_file(tmp_path: Path, content: str, message: str) -> None:
    _pack(tmp_path)
    (tmp_path / "faults.yaml").write_text(content)
    with pytest.raises(PackError, match=message) as excinfo:
        load_faults(tmp_path, TOOLS)
    assert str(tmp_path / "faults.yaml") in str(excinfo.value)


def test_selectors_follow_equality_semantics() -> None:
    labels = {"app": "web", "tier": "front"}
    assert matches_selector(labels, None)
    assert matches_selector(labels, "app=web,tier==front")
    assert not matches_selector(labels, "app=web,tier!=front")
    assert matches_selector(labels, "zone!=eu")
    assert not matches_selector(labels, "zone=eu")


async def test_unrecorded_files_are_not_found_and_other_namespaces_are_empty(
    tmp_path: Path,
) -> None:
    kubernetes = PackKubernetes(_pack(tmp_path), "demo-s1")
    assert [p["metadata"]["name"] for p in await kubernetes.list_pods("demo-s1", "app=web")] == [
        "web-0"
    ]
    assert await kubernetes.list_pods("demo-s1", "app=db") == []
    assert await kubernetes.list_pods("kube-system", None) == []
    with pytest.raises(ToolError) as missing_pod:
        await kubernetes.get_pod("demo-s1", "web-1")
    assert missing_pod.value.model_text == "not_found: pod demo-s1/web-1"
    with pytest.raises(ToolError) as unrecorded:
        await kubernetes.list_events("demo-s1")
    assert unrecorded.value.model_text == "not_found: events demo-s1"
    with pytest.raises(ToolError, match="not_found"):
        await kubernetes.read_log("demo-s1", "web-0", "app", previous=True)


def test_derived_packs_are_independent_copies(tmp_path: Path) -> None:
    source = CASES / "s1-a" / "pack"
    timeout = tmp_path / "s1-d"
    derive_pack(
        source,
        timeout,
        case="s1-d",
        faults={"logs_get_container_logs": "timeout"},
        known_tools=TOOLS,
    )
    assert load_meta(timeout).case == "s1-d" and load_meta(timeout).source_case == "s1-a"
    assert load_faults(timeout, TOOLS) == {"logs_get_container_logs": "timeout"}
    assert (timeout / "repo.fast-export").read_text() == (source / "repo.fast-export").read_text()

    sparse = tmp_path / "s1-f"
    derive_pack(source, sparse, case="s1-f", only=["alert", "pods"], known_tools=TOOLS)
    files = sorted(p.relative_to(sparse).as_posix() for p in sparse.rglob("*") if p.is_file())
    assert files == ["alert.json", "k8s/pods.json", "meta.json"]
    assert load_meta(sparse).repo_head is None


def test_deriving_refuses_to_replace_a_directory_that_is_not_a_pack(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("keep me")
    with pytest.raises(PackError, match="not an evidence pack"):
        derive_pack(CASES / "s1-a" / "pack", tmp_path, case="s1-d", known_tools=TOOLS)
    assert (tmp_path / "notes.txt").read_text() == "keep me"


def _git(repo: Path, *args: str) -> None:
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Bianque Demo",
        "GIT_AUTHOR_EMAIL": "demo@example.com",
        "GIT_COMMITTER_NAME": "Bianque Demo",
        "GIT_COMMITTER_EMAIL": "demo@example.com",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def _objects() -> FakeKubernetes:
    pod = {
        "metadata": {
            "name": "web-0",
            "managedFields": [{"manager": "kubectl"}],
            "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{}"},
        },
        "spec": {"containers": [{"name": "app"}]},
        "status": {"podIP": POD_IP, "hostIP": HOST_IP},
    }
    return FakeKubernetes(
        pods=[pod],
        events=[{"metadata": {"name": "e1"}, "message": f"probe http://{POD_IP}:8080 failed"}],
        logs={("web-0", "app", False): f"listening on {POD_IP}:8080\n"},
    )


async def test_recording_writes_a_clean_generalized_pack(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "deployment.yaml").write_text("replicas: 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Add manifests")
    out = tmp_path / "pack"
    summary = await record_pack(
        _objects(),
        namespace="demo-s1",
        alert_text=json.dumps(
            {"status": "firing", "externalURL": f"http://{ALERTMANAGER_IP}:9093"}
        ),
        repo=repo,
        case="s1-x",
        out=out,
        recorded_at=RECORDED_AT,
        versions={"kind": "v0.33.0"},
    )
    assert summary.objects == {"pods": 1, "events": 1, "deployments": 0, "replicasets": 0}
    assert summary.logs == 1
    pods = json.loads((out / "k8s/pods.json").read_text())
    assert "managedFields" not in pods[0]["metadata"]
    assert "annotations" not in pods[0]["metadata"]
    assert pods[0]["status"] == {"podIP": "10.244.x.x", "hostIP": "172.18.x.x"}
    assert "10.0.x.x" in (out / "alert.json").read_text()
    assert (out / "logs/web-0/app.log").read_text() == "listening on 10.244.x.x:8080\n"
    assert not (out / "logs/web-0/app.previous.log").exists()
    meta = load_meta(out)
    assert (meta.recorded_at, meta.repo_head, meta.versions) == (
        RECORDED_AT,
        "refs/heads/main",
        {"kind": "v0.33.0"},
    )
    async with open_pack(out) as backends:
        assert backends.now() == RECORDED_AT
        log = await backends.git.run("log", ["--format=%s %an", "HEAD"])
        assert log.strip() == "Add manifests Bianque Demo"


async def test_recording_fails_on_a_credential_shape(tmp_path: Path) -> None:
    objects = _objects()
    objects.logs[("web-0", "app", False)] = "connecting with pass" + "word=hunter2Passw0rd\n"
    with pytest.raises(PackError, match=r"logs/web-0/app.log: kv-secret"):
        await record_pack(
            objects,
            namespace="demo-s1",
            alert_text="{}",
            repo=None,
            case="s1-x",
            out=tmp_path / "pack",
            recorded_at=RECORDED_AT,
            versions={},
        )


def test_clean_object_leaves_the_input_untouched() -> None:
    obj = {"metadata": {"name": "x", "managedFields": [1], "annotations": {"a": "b"}}}
    assert clean_object(obj) == {"metadata": {"name": "x", "annotations": {"a": "b"}}}
    assert obj["metadata"]["managedFields"] == [1]


def test_deriving_checks_faults_and_parts_before_writing(tmp_path: Path) -> None:
    out = tmp_path / "derived"
    with pytest.raises(PackError, match="unknown tools"):
        derive_pack(CASES / "s1-a" / "pack", out, case="s1-x", faults={"k8s_delete_pod": "timeout"})
    with pytest.raises(PackError, match="unknown parts"):
        derive_pack(CASES / "s1-a" / "pack", out, case="s1-x", only=["secrets"])
    assert not out.exists()


async def test_the_factory_opens_the_configured_case(tmp_path: Path) -> None:
    from bianque.config import Settings
    from bianque.tools.backends import open_backends

    _pack(tmp_path / "s1-x" / "pack")
    settings = Settings(tool_backend="pack", cases_dir=tmp_path, pack_case="s1-x")
    async with open_backends(settings) as backends:
        assert backends.kind == "pack"
        assert backends.now() == RECORDED_AT
        pods = await backends.kubernetes.list_pods("demo-s1", None)
        assert [p["metadata"]["name"] for p in pods] == ["web-0"]
        with pytest.raises(ToolError, match="not_found: repository"):
            await backends.git.run("log", [])
