"""Recognition of write commands."""

from __future__ import annotations

import pytest

from bianque.gate.forbidden import _COMMANDS, find_write_commands


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
        ("**kubectl delete pod x**", ["kubectl delete"]),
        ("*kubectl delete pod x*", ["kubectl delete"]),
        ("执行kubectl delete pod x", ["kubectl delete"]),
        ("（kubectl delete pod x）", ["kubectl delete"]),
        ("可执行：kubectl scale deploy/x --replicas=0", ["kubectl scale"]),
        ("运行 kubectl delete，然后观察", ["kubectl delete"]),
        ("kubectl delete—then", ["kubectl delete"]),
        ('kubectl -n "demo-s1" delete pod x', ["kubectl delete"]),
        ('kubectl --context="kind" apply -f a.yaml', ["kubectl apply"]),
        ("kubectl --cache-dir /tmp/kube delete pod x", ["kubectl delete"]),
        ("oc delete pod x", ["oc delete"]),
        ("kubectl certificate approve csr-1", ["kubectl certificate approve"]),
        ("git commit -am fix", ["git commit"]),
        ("git revert 3f9a1c2b7d4e", ["git revert"]),
        ("git commit -m 3f9a1c2b", ["git commit"]),
        ("kubectl\u3000delete pod x", ["kubectl delete"]),
        ("kubectl\u00a0delete pod x", ["kubectl delete"]),
        ("kubectl -n demo-s1 \\\n  delete pod x", ["kubectl delete"]),
        ("git commit -am deadbeef1", ["git commit"]),
        ("Run the kubectl delete pod command.", ["kubectl delete"]),
        ("Execute the git push now", ["git push"]),
        ("then use a kubectl apply -f fix.yaml", ["kubectl apply"]),
        ("执行这次 kubectl delete pod x", ["kubectl delete"]),
        ("你应该 kubectl delete pod x", ["kubectl delete"]),
        ("Do a kubectl rollout restart deployment/s1-app", ["kubectl rollout restart"]),
        # After a determiner, a command with arguments is still an instruction.
        ("Mitigate via a kubectl rollout undo deployment/s1-app.", ["kubectl rollout undo"]),
        ("Next step: the kubectl rollout undo deployment/s1-app", ["kubectl rollout undo"]),
        ("then apply the kubectl apply -f fix.yaml", ["kubectl apply"]),
        ("Fix it with a kubectl delete pod x", ["kubectl delete"]),
        ("The kubectl delete pod s1-app-abc will fix it.", ["kubectl delete"]),
        ("Consider a git push --force", ["git push"]),
        ("Recommend the git push origin main", ["git push"]),
        ("用这条 kubectl rollout undo deployment/x 回滚", ["kubectl rollout undo"]),
        ("通过这次 helm rollback s1 3 恢复", ["helm rollback"]),
        ("接下来该 kubectl rollout undo", ["kubectl rollout undo"]),
        ("本该 kubectl delete", ["kubectl delete"]),
        ("请这次 git push", ["git push"]),
        ("运行这条 kubectl delete", ["kubectl delete"]),
        ("Try the kubectl delete", ["kubectl delete"]),
        ("Mitigate with the kubectl rollout undo of deployment/s1-app", ["kubectl rollout undo"]),
        ("the helm rollback of s1 to revision 3", ["helm rollback"]),
        ("the kubectl delete of pod x", ["kubectl delete"]),
        ("the kubectl apply action -f fix.yaml", ["kubectl apply"]),
        ("the kubectl rollout undo (deployment/s1-app)", ["kubectl rollout undo"]),
        ("the kubectl rollout undo `deployment/s1-app`", ["kubectl rollout undo"]),
        ("the kubectl rollout undo: deployment/s1-app", ["kubectl rollout undo"]),
        ("Revert with the kubectl rollout undo, deployment/s1-app", ["kubectl rollout undo"]),
        ("the kubectl rollout undo → s1-app", ["kubectl rollout undo"]),
        ("**the kubectl rollout undo** deployment/x", ["kubectl rollout undo"]),
        ("这次 kubectl rollout undo 回滚 deployment/x", ["kubectl rollout undo"]),
        ("the git revert 3f9a1c2b7d4e", ["git revert"]),
        ("Apply the kubectl set env APP_MODE=standard", ["kubectl set"]),
        ("the kubectl delete pod s1-app-x (ev_9cfa6c69db)", ["kubectl delete"]),
        ("the kubectl apply step with deployment.yaml", ["kubectl apply"]),
        ("the kubectl delete of pod/web", ["kubectl delete"]),
        (
            "Mitigate with the kubectl rollout undo recommended for deployment/s1-app.",
            ["kubectl rollout undo"],
        ),
        ("the git push required for origin main --force", ["git push"]),
        ("the kubectl delete needed for pod s1-app-x", ["kubectl delete"]),
        ("The fix: the\nkubectl delete pod x", ["kubectl delete"]),
        # A verb of need or advice after a command makes it a command to run, and so does
        # a preposition that introduces what it acts on; a target is not needed for that.
        ("the git push required for origin main", ["git push"]),
        ("the git push needed for origin main", ["git push"]),
        ("the kubectl delete recommended for pod x", ["kubectl delete"]),
        ("the kubectl delete should fix it", ["kubectl delete"]),
        ("the git push to origin main", ["git push"]),
        ("the kubectl delete on pod web", ["kubectl delete"]),
        ("the kubectl delete in namespace prod", ["kubectl delete"]),
        ("the helm rollback followed by a restart", ["helm rollback"]),
        # A predicate or a time preposition does not lift the other conditions.
        ("Run the kubectl delete at 10:42", ["kubectl delete"]),
        ("the kubectl delete failed pod s1-app-x", ["kubectl delete"]),
        ("The helm upgrade failed with --force", ["helm upgrade"]),
        ("the kubectl apply ran at 10:42 -f fix.yaml", ["kubectl apply"]),
        (
            "The helm upgrade failed; the helm upgrade s1 ./chart fixes it.",
            ["helm upgrade"],
        ),
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
        "The git commit 3f9a1c2b7d4e changed APP_MODE.",
        "GitHub delete button",
        "kubectl get pods。然后 delete 它",
        "ad hoc delete",
        "git 仓库最近一次 commit 把 APP_MODE 改为 invalid。",
        "清单仓的 git 历史显示，最近一次 commit 修改了 APP_MODE",
        "变更来自 git 仓库的一次 merge",
        "使用kubectl获取信息后，再决定是否 scale",
        "Check the state with kubectl. Delete nothing.",
        "The change came through Helm. Upgrade notes are in the runbook.",
        "kubectl, then delete",
        "kubectl\ndelete pod x",
        # A command word right after a determiner names a thing; these are report texts
        # from a live run, where the model described missing evidence.
        "The git commit and ReplicaSet timestamps align, but the apply step was not captured.",
        "Confirmation of who applied the manifest change (the git commit author is known).",
        "The evidence shows the git commit but not the kubectl apply action that triggered it.",
        "Its helm upgrade history is not available.",
        "No record of the kubectl apply.",
        "这个 helm upgrade 的记录缺失",
        # Descriptive report prose with evidence IDs, times, versions and kinds.
        "The git commit author is known (ev_9cfa6c69db).",
        "The git commit and ReplicaSet timestamps align at 10:42 UTC.",
        "The git commit and revision 2 align.",
        "The git commit isn't recorded.",
        "The git commit author's identity is in ev_9cfa6c69db.",
        "The kubectl apply action between ReplicaSet/Deployment revisions is not captured.",
        "The kubectl apply action: not captured",
        "这次 kubectl apply 操作发生在 10:42，与 revision 2 对应。",
        "该 git commit 的作者已知（ev_9cfa6c69db）。",
        "该次 kubectl apply 操作没有留下证据。",
        "The git commit timestamp is 2026-10-20T10:42:00Z.",
        "The git commit author is known; the kubectl apply action ran at 10:42:07Z.",
        "The git commit and the v1.2.3 image align.",
        "The git commit and/or the rollout changed the read/write settings.",
        "此次 git commit 的作者已知，但这次 kubectl apply 操作没有留下证据。",
        # A predicate that describes what happened, or a time preposition, after a command
        # used as a noun.
        "The git commit changed APP_MODE",
        "The helm upgrade failed.",
        "The git commit at 10:42:07Z is recorded.",
        "The helm upgrade failed to complete.",
        "The kubectl apply ran at 10:42.",
        "The git commit changed APP_MODE from standard to invalid (ev_9cfa6c69db).",
        "The kubectl apply happened before the restarts began.",
        "The git commit introduces the invalid value, and the kubectl apply triggers the rollout.",
        "The helm upgrade succeeded at 10:40, after the git commit landed.",
        "这次 helm upgrade 失败了。",
    ],
)
def test_read_commands_and_prose_are_not_write_commands(text: str) -> None:
    assert find_write_commands(text) == []


def _table() -> list[str]:
    commands = []
    for name, command in _COMMANDS.items():
        commands += [f"{name} {verb}" for verb in command.verbs]
        for verb, subs in command.sub_verbs.items():
            commands += [f"{name} {verb} {sub}" for sub in subs]
    return sorted(commands)


@pytest.mark.parametrize("command", _table())
def test_every_verb_of_the_table_is_found(command: str) -> None:
    name, rest = command.split(" ", 1)
    option = "--namespace=demo-s1" if name != "git" else "-C /repo"
    assert find_write_commands(f"Then run `{name} {option} {rest} target`.") == [command]
