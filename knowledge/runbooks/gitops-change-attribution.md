# Attributing a Fault to a GitOps Commit

When Kubernetes manifests are kept in a Git repository and a sync tool applies them to the cluster, every intended change to a workload has a commit. Finding the commit that introduced a fault means narrowing the history to the right files, reading the diffs, and matching commit times to when the symptoms began.

## Why the manifest repository is the source of truth

In a GitOps setup, the cluster state is derived from the repository. A sync controller compares the manifests at the tracked branch with what runs in the cluster and applies the difference, either continuously or on a schedule.

For diagnosis this has two consequences:

- The commit history records who changed what and when, with a message explaining why. That is richer than the Deployment's rollout history, which shows what changed in the Pod template but not who changed it or why.
- Changes made directly in the cluster, such as hand edits to a live object, are drift. The sync controller may overwrite them, and they leave no commit. If the cluster differs from the repository, report the drift rather than attributing the fault to a commit.

Before searching the history, identify which path in the repository holds the affected workload. Repositories are often organized per application and environment, such as `deploy/web/` with a `deployment.yaml`, a `configmap.yaml`, and an overlay per environment.

## Narrowing the log to the manifest path

Limit the log to the files that define the failing workload. This removes commits that cannot have affected it:

```
git log --oneline -- deploy/web/
git log --since="2 hours ago" --format="%h %ad %an %s" --date=iso -- deploy/web/
git log -n 10 --stat -- deploy/web/deployment.yaml
```

`--stat` shows which files each commit touched and how many lines changed. Use `--date=iso` so commit times can be compared with Kubernetes timestamps, which are in UTC, and convert time zones explicitly if the two differ.

Include every file that feeds the workload: the Deployment, its ConfigMaps and Secrets references, kustomization files, and Helm values files. A commit that changes only `values.yaml` can still change the rendered Deployment.

Prefer the time the commit reached the tracked branch over the author date. A commit authored yesterday and merged ten minutes ago took effect ten minutes ago. `git log --first-parent` on the branch shows merges in the order they landed.

## Reading the diff of a candidate commit

For each candidate, read the change itself:

```
git show <commit>
git show <commit> -- deploy/web/deployment.yaml
git show --stat <commit>
```

Look for changes to fields that commonly break a workload:

- Environment variable values or ConfigMap keys, such as `LISTEN_PORT: "8080"` becoming `LISTEN_PORT: "eighty"`.
- `resources.limits.memory` lowered below what the application needs.
- Probe path, port, or timing.
- Image tag or repository.
- `command`, `args`, volume mounts, or ServiceAccount.

Compare what the diff introduced with the evidence from the cluster. A diff that sets a value which then appears word for word in the crash log, or a lowered limit followed by `OOMKilled` terminations, is strong evidence. A diff that touches a field unrelated to the observed error is weak evidence even if its timing fits.

## Matching commit time to when restarts began

Timing turns a plausible commit into the likely cause. Line up:

1. The commit time, or merge time on the tracked branch.
2. The sync time, from the sync controller's status or the creation time of the new ReplicaSet.
3. The first failure, from the earliest restart or `BackOff` event in the namespace.

The expected order is commit, then sync within the controller's interval, then the first failure seconds to minutes later. A commit that lands after restarts began cannot be the cause. If several commits landed in the window, the one whose diff matches the error message wins over the one closest in time.

If no commit precedes the fault, the cause is outside the repository: a dependency, a node, an expired credential, or drift introduced directly in the cluster.

## Ignoring commits that touch unrelated files

Not every commit in the window matters. Exclude commits that cannot change what runs in the cluster:

- Documentation such as `README.md` or `CHANGELOG.md`.
- CI pipeline definitions, editor settings, and lint configuration.
- Manifests of other applications or other environments than the one that is failing.
- Comment-only or formatting-only edits to YAML, which render to the same object.

`git show --stat <commit>` answers this quickly. A commit whose only changed file is `README.md` must not be reported as the cause even if its timestamp sits right before the first restart. Mentioning it as a considered and excluded candidate is useful; attributing the fault to it is wrong.

To list only commits that touched files outside documentation:

```
git log --oneline -- deploy/web/ ':(exclude)*.md'
```

## Remediation: reverting the commit through Git

Reverting changes the source of truth and, after sync, the cluster. It needs human confirmation before it is applied.

- Revert the faulty commit with `git revert <commit>` on a branch, open a review if the repository requires one, and `git push`. The sync controller then applies the previous manifest. This keeps the history honest and the cluster aligned with the repository.
- Do not use `kubectl rollout undo` or `kubectl apply` as the fix in a GitOps setup. The next sync reapplies the faulty manifest, and the cluster drifts from the repository in the meantime.
- If the commit also contains unrelated changes that must stay, revert only the faulty part in a new commit instead of reverting everything.
- After the sync, confirm that new Pods become ready and the alert resolves.
