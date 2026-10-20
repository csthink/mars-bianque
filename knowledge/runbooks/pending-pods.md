# Pods Stuck in Pending

A Pod in `Pending` has been accepted by the API server but has not started running. Most often the scheduler cannot find a node that satisfies the Pod's requirements, and the reason is spelled out in a `FailedScheduling` event.

## What Pending covers

`Pending` spans everything between creation and the first container running. Two very different situations share the status:

- **Not scheduled**: the Pod has no node assigned. `kubectl get pod -o wide` shows `<none>` in the NODE column, and the `PodScheduled` condition is `False`. This runbook focuses on this case.
- **Scheduled but not started**: a node is assigned, but the containers are waiting, for example on an image pull, a volume mount, or an init container. The container status, not the scheduler, explains these.

```
kubectl get pods -n demo -o wide --field-selector status.phase=Pending
kubectl describe pod web-5c7b9d8f6-q2w3e -n demo
```

In the `describe` output, the Conditions block and the Events section at the end are what matter. A Pending Pod never restarts and produces no logs, so restart-count and log-based checks do not apply.

## Reading FailedScheduling events

When the scheduler cannot place a Pod, it records a `Warning` event with reason `FailedScheduling`. The message summarizes why each node was rejected, for example:

`0/5 nodes are available: 2 Insufficient memory, 2 node(s) had untolerated taint {dedicated: batch}, 1 node(s) didn't match Pod's node affinity/selector. preemption: 0/5 nodes are available: 5 Preemption is not helpful for scheduling.`

How to read it:

- The first number is how many nodes fit; the second is how many were considered.
- Each clause lists a reason and how many nodes it eliminated. The counts add up to the total.
- The preemption part says whether evicting lower-priority Pods would help.

```
kubectl get events -n demo --field-selector reason=FailedScheduling
```

The scheduler retries as cluster state changes, so the event `count` grows and the `lastTimestamp` moves forward while the problem persists. If an autoscaler is installed, a `TriggeredScaleUp` or `NotTriggerScaleUp` event next to it says whether a new node is on the way.

## Insufficient CPU or memory

`Insufficient cpu` or `Insufficient memory` means no node has enough unreserved capacity for the Pod's requests. Scheduling uses requests, not actual usage, so a node that looks idle can still be full on paper.

```
kubectl get deployment web -n demo -o jsonpath='{.spec.template.spec.containers[*].resources.requests}'
kubectl describe nodes | grep -A 8 "Allocated resources"
```

Compare the Pod's requests with each node's allocatable capacity minus what is already requested. Typical causes:

- A request was raised in a recent change, for example from `250m` to `4` CPUs, beyond what any single node offers.
- A unit mistake, such as `memory: 8G` written instead of `800Mi`.
- The cluster is genuinely full after a scale-up of replicas.
- A ResourceQuota in the namespace, which shows up as a creation failure on the ReplicaSet rather than as a Pending Pod.

If the requests exceed the allocatable capacity of the largest node, adding more nodes of the same size will not help.

## Node selectors and affinity

`didn't match Pod's node affinity/selector` means the Pod requires node labels that no available node has.

```
kubectl get deployment web -n demo -o jsonpath='{.spec.template.spec.nodeSelector}'
kubectl get deployment web -n demo -o jsonpath='{.spec.template.spec.affinity}'
kubectl get nodes --show-labels
```

Check for:

- A `nodeSelector` with a label key or value that no node carries, often a typo or a label from another cluster.
- `requiredDuringSchedulingIgnoredDuringExecution` node affinity rules that are stricter than intended. `preferred` rules never block scheduling.
- Pod anti-affinity that requires one replica per node or zone, with more replicas than nodes or zones. This appears as `didn't match pod anti-affinity rules`.
- Topology spread constraints with `whenUnsatisfiable: DoNotSchedule`, which can block placement when zones are unbalanced.

## Taints and tolerations

`had untolerated taint` means the matching nodes carry a taint the Pod does not tolerate. Taints with effect `NoSchedule` keep new Pods off a node unless the Pod has a matching toleration.

```
kubectl get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints
kubectl get deployment web -n demo -o jsonpath='{.spec.template.spec.tolerations}'
```

Situations to recognize:

- Nodes reserved for a workload class, such as `dedicated=batch:NoSchedule`, while the Pod lacks the toleration.
- Nodes that Kubernetes tainted automatically, such as `node.kubernetes.io/not-ready`, `node.kubernetes.io/disk-pressure`, or `node.kubernetes.io/unschedulable` after a cordon. If every node has one of these, the problem is node health, not the Pod.
- A toleration with a key, value, or effect that does not match the taint exactly.

## Unbound PersistentVolumeClaims

A Pod that mounts a PersistentVolumeClaim cannot be scheduled until the claim can be bound. The event reads `pod has unbound immediate PersistentVolumeClaims` or mentions volume node affinity conflicts.

```
kubectl get pvc -n demo
kubectl describe pvc data-web-0 -n demo
kubectl get storageclass
```

Check the claim's status and events:

- `Pending` with `no persistent volumes available` and no StorageClass: there is no default StorageClass, or the named one does not exist.
- Provisioning errors from the storage driver, shown as `ProvisioningFailed` events.
- A volume bound in one zone while the Pod's constraints point to another, which causes `volume node affinity conflict`.
- Claims using `WaitForFirstConsumer` stay `Pending` until a Pod is scheduled; that is normal and not a fault by itself.

## Remediation: making the Pod schedulable

Each option changes the workload, the nodes, or the cluster capacity and needs human confirmation before it is applied.

- If a recent change raised requests or added a selector, revert it in the manifest source (`git revert <commit>` and `git push`), or with `kubectl rollout undo deployment/web -n demo`.
- If the cluster is out of capacity, add nodes or reduce replicas with `kubectl scale deployment/web -n demo --replicas=<n>`.
- Correct tolerations, selectors, or storage classes in the manifest and apply with `kubectl apply -f`, rather than removing taints from shared nodes.
