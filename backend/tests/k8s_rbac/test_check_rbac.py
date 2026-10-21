"""The RBAC check accepts read-only rules and rejects every way of granting more."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from tools.check_rbac import check_documents

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent


def _role(*rules: dict[str, Any], kind: str = "ClusterRole") -> dict[str, Any]:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": kind,
        "metadata": {"name": "bianque-read"},
        "rules": list(rules),
    }


READ_PODS = {"apiGroups": [""], "resources": ["pods", "pods/log"], "verbs": ["get", "list"]}
BINDING = {
    "apiVersion": "rbac.authorization.k8s.io/v1",
    "kind": "RoleBinding",
    "metadata": {"name": "bianque-read", "namespace": "demo-s1"},
}


def test_read_only_rules_pass() -> None:
    watch = {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["watch"]}
    assert check_documents([_role(READ_PODS, watch), _role(READ_PODS, kind="Role"), BINDING]) == []


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ({**READ_PODS, "verbs": ["get", "create"]}, "verb 'create'"),
        ({**READ_PODS, "verbs": ["*"]}, "wildcard in verbs"),
        ({**READ_PODS, "resources": ["*"]}, "wildcard in resources"),
        ({**READ_PODS, "apiGroups": ["*"]}, "wildcard in apiGroups"),
        ({**READ_PODS, "verbs": ["get", "escalate"]}, "verb 'escalate'"),
        ({"nonResourceURLs": ["/metrics"], "verbs": ["get"]}, "nonResourceURLs"),
        ({**READ_PODS, "verbs": []}, "no verbs"),
    ],
)
def test_a_rule_granting_more_than_reading_fails(rule: dict[str, Any], expected: str) -> None:
    violations = check_documents([_role(rule)])
    assert len(violations) == 1
    assert expected in violations[0]
    assert "ClusterRole bianque-read rule 1" in violations[0]


def test_a_cluster_role_binding_fails() -> None:
    binding = {**BINDING, "kind": "ClusterRoleBinding"}
    assert check_documents([_role(READ_PODS), binding]) == [
        "ClusterRoleBinding bianque-read: grants a role in every namespace"
    ]


def test_rendering_without_any_role_fails() -> None:
    assert check_documents([BINDING, None]) == ["no Role or ClusterRole rendered"]


def test_the_command_reads_the_rendering_on_standard_input() -> None:
    good = yaml.safe_dump_all([_role(READ_PODS), BINDING])
    bad = yaml.safe_dump_all([_role({**READ_PODS, "verbs": ["delete"]})])
    passed = _run(good)
    assert passed.returncode == 0, passed.stderr
    failed = _run(bad)
    assert failed.returncode == 1
    assert "verb 'delete'" in failed.stderr


def _run(rendering: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tools.check_rbac"],
        cwd=BACKEND_ROOT,
        input=rendering,
        capture_output=True,
        text=True,
        check=False,
    )
