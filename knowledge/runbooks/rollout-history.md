# Deployment Rollout History

Every change to a Deployment's Pod template creates a new revision, backed by its own ReplicaSet. Comparing adjacent revisions shows exactly what changed in the Pod template and approximately when, which is often the fastest way to connect a new fault to the change that caused it.

## Revisions and ReplicaSets

A Deployment does not run Pods directly. It owns ReplicaSets, and each ReplicaSet owns Pods built from one fixed Pod template. When the Pod template changes, the Deployment controller creates a new ReplicaSet and shifts replicas from the old one to the new one according to the rollout strategy.

Key facts:

- Only changes under `spec.template` create a new revision. Scaling `replicas` or changing the strategy does not.
- Each ReplicaSet name ends in a hash of its Pod template, such as `web-7d9c8b6f5`. Pod names start with that same prefix, so a Pod's name tells you which revision it belongs to.
- Old ReplicaSets are kept with zero replicas so the Deployment can roll back. `spec.revisionHistoryLimit` (default 10) controls how many are kept; older revisions disappear from the history.
- Rolling back to an old template does not reuse its revision number. The old ReplicaSet is scaled up again and receives the next revision number.

```
kubectl get replicasets -n demo -l app=web
kubectl get pods -n demo -l app=web
```

## The revision annotation

The revision number is stored as the annotation `deployment.kubernetes.io/revision` on both the Deployment and each ReplicaSet. The Deployment's value is the current revision; each ReplicaSet's value is the revision it represents.

```
kubectl get deployment web -n demo -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}'
kubectl get replicasets -n demo -l app=web -o custom-columns=NAME:.metadata.name,REVISION:.metadata.annotations.deployment\.kubernetes\.io/revision,DESIRED:.spec.replicas,CREATED:.metadata.creationTimestamp
```

The second command is useful for building a timeline: the ReplicaSet `creationTimestamp` is close to when that revision was first rolled out. For a revision that was rolled back to and then forward again, the creation time is when the template first appeared, not the most recent activation.

The `kubernetes.io/change-cause` annotation, if a team sets it, appears in the CHANGE-CAUSE column of the history and records why the change was made. It is free text and may be absent or stale.

## Listing the rollout history

`kubectl rollout history` lists the revisions still retained:

```
kubectl rollout history deployment/web -n demo
```

The output has a REVISION column and a CHANGE-CAUSE column. It does not show timestamps; use the ReplicaSet creation times for those.

To see the full Pod template of one revision:

```
kubectl rollout history deployment/web -n demo --revision=7
```

This prints labels, annotations, containers with their image, ports, command, environment variables, resource limits and requests, probes, and volumes for that revision.

Check the rollout state at the same time:

```
kubectl rollout status deployment/web -n demo --timeout=10s
kubectl describe deployment web -n demo
```

In `describe`, `OldReplicaSets` and `NewReplicaSet` show which revisions currently own Pods. The conditions show `Progressing` with reason `ProgressDeadlineExceeded` when the new revision never became available.

## Comparing two adjacent revisions

To find what a rollout changed, print the current and previous revisions and compare them field by field:

```
kubectl rollout history deployment/web -n demo --revision=6
kubectl rollout history deployment/web -n demo --revision=7
```

Saving both outputs to files and running `diff` on them gives a compact view. Focus on the fields that commonly cause faults:

- **Image**: a new tag or registry path.
- **Environment**: added, removed, or changed variables, and changed ConfigMap or Secret references.
- **Limits and Requests**: lowered memory limits lead to `OOMKilled`; raised requests can leave Pods Pending.
- **Liveness, Readiness, and Startup probes**: changed path, port, or timing.
- **Command and Args**: a changed entrypoint or flag.

If the two revisions are identical except for an annotation such as a restart timestamp, the rollout only restarted the Pods. The fault then comes from something outside the Pod template, such as a ConfigMap, a Secret, or a dependency.

## Matching a revision to when the fault began

A changed field is a suspect; matching times makes it the likely cause. Build the timeline from three sources:

1. The creation time of the new ReplicaSet, from the custom-columns command above.
2. The first restart or failure in namespace events, from `kubectl get events -n demo --sort-by=.metadata.creationTimestamp`.
3. The start time of the oldest Pod of the new revision.

The new revision is the likely trigger when the first failure follows its creation by seconds to a few minutes and only Pods of that revision fail. Pods of the old ReplicaSet that keep running without errors make the case stronger.

If failures began before the new ReplicaSet existed, or Pods of both revisions fail in the same way, the rollout is not the cause. Look at shared dependencies, nodes, and configuration objects instead.

## Remediation: rolling back to a previous revision

Rolling back changes the running workload and needs human confirmation before it is applied.

- Roll back to the immediately previous revision with `kubectl rollout undo deployment/web -n demo`, or to a specific one with `kubectl rollout undo deployment/web -n demo --to-revision=6`.
- In a GitOps setup, a rollback made directly in the cluster is overwritten at the next sync. Revert the commit in the manifest repository instead (`git revert <commit>` and `git push`), so the source of truth and the cluster agree.
- After the rollback, confirm that the new Pods become ready and that restarts stop.
