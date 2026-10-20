# OOMKilled and Exit Code 137

A container terminated with reason `OOMKilled` used more memory than its cgroup allowed and was killed by the Linux kernel. This runbook explains how memory requests and limits relate to that kill and how to confirm whether the limit is simply too small or the workload's memory use has grown.

## How the out-of-memory kill happens

Each container runs inside a memory cgroup. When the container has a memory limit, the cgroup enforces it as a hard ceiling. If the processes in the container try to use more than that ceiling and the kernel cannot reclaim enough page cache, the kernel's out-of-memory killer sends SIGKILL to a process in the cgroup. The process cannot catch or handle SIGKILL, so it gets no chance to log a final message or shut down cleanly.

The kubelet records the result in the container status:

- `Last State: Terminated`
- `Reason: OOMKilled`
- `Exit Code: 137` (128 + signal 9)

With `restartPolicy: Always`, the container restarts. If the memory use pattern repeats on every start, the Pod ends up in CrashLoopBackOff.

A node can also run short of memory overall. In that case the kubelet may evict whole Pods (the Pod status shows `Evicted` with a message about memory pressure), which is a different event from a per-container cgroup kill.

## Memory requests versus limits

The two fields do different jobs:

- **requests.memory** is what the scheduler reserves on a node. It decides placement and influences eviction ranking, but it does not cap usage.
- **limits.memory** is the cgroup ceiling. Crossing it triggers the out-of-memory kill.

A container with a request but no limit can use free memory on the node until the node itself runs low. A container whose limit equals its request (and whose CPU request equals its limit) gets the `Guaranteed` quality-of-service class and is the last to be evicted under node pressure.

Common configurations that lead to `OOMKilled`:

- A limit copied from another service without measuring this one.
- A runtime that sizes its heap from the host instead of the container, such as an older JVM, or a heap flag set larger than the container limit.
- A limit lowered in a recent change, for example from `512Mi` to `64Mi`, while the application still needs the old amount at start-up.
- A genuine leak, where memory grows steadily over hours until the limit is reached.

## Confirming the termination reason

Check that the kill really came from the memory limit before changing anything:

```
kubectl describe pod web-7d9c8b6f5-abcde -n demo
kubectl get pod web-7d9c8b6f5-abcde -n demo -o jsonpath='{.status.containerStatuses[*].lastState.terminated.reason}'
```

`OOMKilled` in the last termination reason is the decisive signal. Exit code 137 alone is not enough: the same code appears when the kubelet force-kills a container that did not stop within its grace period, for example after liveness probe failures.

Then look at the timing:

- `Started` and `Finished` only seconds apart means the application exceeds the limit during start-up. The limit is too small for the program as configured.
- Hours between start and kill, with usage rising steadily, suggests a leak or growing workload.

If a metrics server is installed, `kubectl top pod -n demo` shows current usage for running containers, which helps compare real consumption with the limit.

## Reading the Deployment resources

Read the configured values straight from the Deployment so the comparison uses what is actually applied:

```
kubectl get deployment web -n demo -o jsonpath='{.spec.template.spec.containers[*].resources}'
kubectl describe deployment web -n demo
```

Compare three numbers:

- The memory limit in the Pod template.
- The memory the application needs at steady state and at start-up, from metrics, load tests, or the previous limit that worked.
- Any heap or cache size set in the container's environment variables or arguments, such as `-Xmx` or a cache size flag. A heap setting at or above the limit guarantees a kill, because the runtime also needs memory outside the heap.

Units matter. `Mi` and `Gi` are powers of two, while `M` and `G` are powers of ten. A typo such as `64Mi` where `640Mi` was intended, or a lowercase `m` (which means thousandths) is easy to miss in a review.

## Finding a recent change to limits

When restarts began suddenly, check whether the limit changed at the same time:

```
kubectl rollout history deployment/web -n demo
kubectl rollout history deployment/web -n demo --revision=4
kubectl rollout history deployment/web -n demo --revision=5
```

Compare the `Limits` lines in the two adjacent revisions. If the newer revision has a smaller memory limit and the first `OOMKilled` termination follows its rollout, the change is the likely cause.

When manifests are kept in Git, run `git log -p` on the Deployment file and look for a diff that touches `resources.limits.memory`. The commit time should precede the first restart by a short interval that matches the sync or deploy delay.

## Remediation: restoring an adequate memory limit

Any change below modifies the cluster or the source of truth and needs human confirmation before it is applied.

- If a recent change lowered the limit, revert that change. With GitOps, revert the commit in Git and let the sync apply it (`git revert <commit>` followed by `git push`). Without GitOps, roll back the Deployment with `kubectl rollout undo deployment/web -n demo --to-revision=4`.
- If no change explains the kill, raise the limit to a measured value with headroom and apply it through the normal manifest path rather than with `kubectl edit`, so the source of truth stays accurate.
- If memory grows without bound, raising the limit only delays the next kill. Treat it as a leak and capture a heap profile before the container is restarted.
