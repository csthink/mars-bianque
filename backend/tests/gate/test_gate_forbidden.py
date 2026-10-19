"""Recognition of write commands."""

from __future__ import annotations

import pytest

from bianque.gate.forbidden import find_write_commands


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("kubectl delete pod s1-app-x", ["kubectl delete"]),
        ("kubectl -n demo-s1 scale deployment/s1-app --replicas=0", ["kubectl scale"]),
        ("kubectl --namespace=demo-s1 patch deployment s1-app", ["kubectl patch"]),
        ("kubectl --context kind-bianque apply -f app.yaml", ["kubectl apply"]),
        ("kubectl edit deployment s1-app", ["kubectl edit"]),
        ("kubectl rollout undo deployment/s1-app", ["kubectl rollout undo"]),
        ("kubectl -n demo-s1 rollout restart deployment/s1-app", ["kubectl rollout restart"]),
        ("kubectl set env deployment/s1-app APP_MODE=normal", ["kubectl set"]),
        ("kubectl exec -it s1-app-x -- sh", ["kubectl exec"]),
        ("helm upgrade s1-app ./chart", ["helm upgrade"]),
        ("helm -n bianque uninstall bianque", ["helm uninstall"]),
        ("helm rollback s1-app 1", ["helm rollback"]),
        ("git push origin main", ["git push"]),
        ("git -C /repo reset --hard HEAD~1", ["git reset"]),
        ("/usr/local/bin/kubectl delete pod x", ["kubectl delete"]),
        ("Run `kubectl delete pod x`, then `git push`.", ["kubectl delete", "git push"]),
        ("First kubectl get pods; then kubectl delete pod x", ["kubectl delete"]),
        ("KUBECTL DELETE POD X", ["kubectl delete"]),
        ("Restart it with kubectl delete.", ["kubectl delete"]),
    ],
)
def test_write_commands_are_found(text: str, expected: list[str]) -> None:
    assert find_write_commands(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "kubectl get pods -n demo-s1",
        "kubectl -n demo-s1 describe pod s1-app-x",
        "kubectl logs s1-app-x --previous",
        "kubectl rollout history deployment/s1-app",
        "kubectl -n demo-s1 rollout status deployment/s1-app",
        "git log --oneline -5",
        "git show 3f9a1c2b7d4e",
        "helm status bianque",
        "Use kubectl to delete nothing; only read.",
        "Do not delete or scale anything.",
        "The deployment was scaled by the autoscaler.",
        "kubectl",
        "",
    ],
)
def test_read_commands_and_prose_are_not_write_commands(text: str) -> None:
    assert find_write_commands(text) == []
