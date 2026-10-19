"""The S1 fault scripts change the manifest repository as intended, and only once.

The scripts run against a temporary ``DEMO_DIR`` with ``KUBECTL=true``, so the
manifest repository logic is exercised without a cluster; applying the manifests on
kind is covered by the manual run recorded for the demo environment. ``HOME`` points
at a git configuration that signs commits and sets another identity, to prove the
machine's own git settings never reach the repository.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "demo" / "scripts"
DEMO_IDENTITY = "Bianque Demo <demo@example.com> Bianque Demo <demo@example.com>"


class Demo:
    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path / "demo"
        self.manifests = self.dir / "manifests"
        home = tmp_path / "home"
        home.mkdir()
        (home / ".gitconfig").write_text(
            "[user]\n\tname = Someone Else\n\temail = someone@example.org\n"
            "[commit]\n\tgpgsign = true\n"
        )
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "DEMO_DIR": str(self.dir),
            "KUBECTL": "true",
        }

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", *args], env=self.env, capture_output=True, text=True, check=check
        )

    def init(self) -> None:
        self.run("-c", f'source "{SCRIPTS}/lib.sh"; init_manifests')

    def inject(self, case: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.run(str(SCRIPTS / "inject-s1.sh"), case, check=check)

    def restore(self) -> subprocess.CompletedProcess[str]:
        return self.run(str(SCRIPTS / "restore-s1.sh"))

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.manifests), *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def commit_count(self) -> int:
        return int(self.git("rev-list", "--count", "HEAD"))

    def diff_from_baseline(self) -> list[str]:
        out = self.git("diff", "--unified=0", "baseline", "HEAD")
        return [
            line
            for line in out.splitlines()
            if line[:1] in "+-" and not line.startswith(("+++", "---"))
        ]

    def at_baseline(self) -> bool:
        return self.git("diff", "baseline", "HEAD") == ""

    def fault_state(self) -> str | None:
        state = self.dir / "state" / "fault"
        return state.read_text().split()[0] if state.exists() else None


@pytest.fixture
def demo(tmp_path: Path) -> Demo:
    d = Demo(tmp_path)
    d.init()
    return d


def test_init_commits_the_templates_once_and_tags_baseline(demo: Demo) -> None:
    assert demo.commit_count() == 1
    assert demo.git("log", "-1", "--format=%s", "baseline").strip() == "Add s1-app manifests"
    templates = sorted(p.name for p in (REPO_ROOT / "demo" / "s1-app" / "manifests").iterdir())
    assert sorted(demo.git("ls-files").split()) == templates
    demo.init()
    assert demo.commit_count() == 1


@pytest.mark.parametrize(
    ("case", "message", "changed"),
    [
        (
            "a",
            "Update app mode configuration",
            ["-              value: standard", "+              value: invalid"],
        ),
        (
            "b",
            "Tune s1-app memory limit",
            [
                "-              memory: 64Mi",
                "+              memory: 32Mi",
                "-              memory: 128Mi",
                "+              memory: 32Mi",
            ],
        ),
        (
            "c",
            "Move liveness check to the status endpoint",
            ["-              path: /healthz", "+              path: /status"],
        ),
    ],
)
def test_single_commit_cases(demo: Demo, case: str, message: str, changed: list[str]) -> None:
    demo.inject(case)
    assert demo.commit_count() == 2
    assert demo.git("log", "-1", "--format=%s").strip() == message
    assert demo.diff_from_baseline() == changed
    assert demo.fault_state() == case


def test_case_c_leaves_the_readiness_probe_alone(demo: Demo) -> None:
    demo.inject("c")
    deployment = (demo.manifests / "deployment.yaml").read_text()
    readiness = deployment.split("readinessProbe:")[1]
    assert "path: /healthz" in readiness


def test_case_e_is_a_fault_commit_followed_by_an_unrelated_commit(demo: Demo) -> None:
    demo.inject("e")
    subjects = demo.git("log", "--format=%s", "baseline..HEAD").splitlines()
    assert subjects == ["Update deployment notes", "Update app mode configuration"]
    assert demo.git("show", "--name-only", "--format=", "HEAD").split() == ["README.md"]
    assert demo.git("show", "--name-only", "--format=", "HEAD~1").split() == ["deployment.yaml"]


def test_injecting_the_same_case_again_adds_no_commit(demo: Demo) -> None:
    demo.inject("a")
    result = demo.inject("a")
    assert "already injected" in result.stdout
    assert demo.commit_count() == 2


def test_injecting_another_case_is_refused(demo: Demo) -> None:
    demo.inject("a")
    result = demo.inject("b", check=False)
    assert result.returncode == 1
    assert "S1-a is injected" in result.stderr
    assert demo.commit_count() == 2
    assert demo.fault_state() == "a"


def test_unknown_case_is_a_usage_error(demo: Demo) -> None:
    result = demo.inject("d", check=False)
    assert result.returncode == 1
    assert "usage" in result.stderr
    assert demo.fault_state() is None


@pytest.mark.parametrize(("case", "reverts"), [("a", 1), ("b", 1), ("c", 1), ("e", 2)])
def test_restore_reverts_to_baseline_once(demo: Demo, case: str, reverts: int) -> None:
    demo.inject(case)
    injected = demo.commit_count()
    demo.restore()
    assert demo.at_baseline()
    assert demo.commit_count() == injected + reverts
    assert demo.fault_state() is None
    demo.restore()
    assert demo.commit_count() == injected + reverts


def test_restore_after_an_interrupted_restore_does_not_reinject(demo: Demo) -> None:
    demo.inject("a")
    demo.run("-c", f'source "{SCRIPTS}/lib.sh"; demo_git revert --no-edit HEAD >/dev/null')
    assert demo.fault_state() == "a"
    commits = demo.commit_count()
    demo.restore()
    assert demo.at_baseline()
    assert demo.commit_count() == commits


def test_refuses_to_work_on_uncommitted_changes(demo: Demo) -> None:
    (demo.manifests / "README.md").write_text("edited\n")
    result = demo.inject("a", check=False)
    assert result.returncode == 1
    assert "uncommitted changes" in result.stderr


def test_every_commit_carries_only_the_demo_identity_and_no_signature(demo: Demo) -> None:
    for case in ("a", "b", "c", "e"):
        demo.inject(case)
        demo.restore()
    identities = set(demo.git("log", "--format=%an <%ae> %cn <%ce>").splitlines())
    assert identities == {DEMO_IDENTITY}
    assert demo.git("log", "--format=%G?").split() == ["N"] * demo.commit_count()
    assert "gpgsig" not in demo.git("cat-file", "-p", "HEAD")
