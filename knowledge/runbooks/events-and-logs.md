# Reading Events and Container Logs

Events and container logs are the two primary sources of evidence for a failing workload. Events record what Kubernetes components observed and did; logs record what the application itself reported. Reading both, with attention to timestamps and repetition, usually narrows an alert to one cause.

## What a Kubernetes event records

An event is a short-lived API object written by a component such as the scheduler, the kubelet, or a controller. The fields that matter for diagnosis:

- **type**: `Normal` or `Warning`. Start with warnings.
- **reason**: a short machine-readable cause, such as `BackOff`, `Unhealthy`, `Killing`, `FailedScheduling`, `Failed`, or `ScalingReplicaSet`.
- **message**: the human-readable detail, often containing the actual error text.
- **involvedObject**: the kind, name, and namespace of the object the event is about, for example a Pod, ReplicaSet, or Deployment.
- **count**: how many times the same event occurred. Repeated identical events are merged into one object with a rising count.
- **firstTimestamp** and **lastTimestamp**: when the event first and most recently occurred.
- **source**: which component reported it.

Events are kept for a limited time, one hour by default, so evidence from an older incident may be gone. Capture them early.

## Listing namespace events

List all events in the affected namespace, sorted by time:

```
kubectl get events -n demo --sort-by=.lastTimestamp
kubectl get events -n demo --field-selector type=Warning
kubectl get events -n demo --field-selector involvedObject.name=web-7d9c8b6f5-abcde
kubectl get events -n demo -o wide
```

`-o wide` adds columns such as the source component, the count, and the first-seen time. `kubectl describe pod` also prints the events for that Pod at the end of its output, which is convenient when the Pod is already known.

Events on related objects tell the rest of the story. The Deployment's events show `ScalingReplicaSet`, which marks when a rollout started. The ReplicaSet's events show Pod creation failures, such as quota rejections, that never appear on a Pod because the Pod was never created.

## Interpreting count and timestamps

The count and the two timestamps distinguish a persistent fault from a one-off:

- A high count with `firstTimestamp` minutes ago and `lastTimestamp` seconds ago means the problem is ongoing and started at the first timestamp.
- A count of 1 with an old `lastTimestamp` is a transient issue that has stopped.
- A `BackOff` event with a rising count confirms a restart loop; the first timestamp approximates when the loop began.

Use `firstTimestamp` of the earliest warning to anchor the incident timeline. Compare it with the creation time of the newest ReplicaSet and with commit times in the manifest repository to see whether a change preceded the fault.

Some clusters report events through the newer events API, where `eventTime` and series counts replace the older fields and `kubectl get events` may show `<unknown>` for a timestamp. In that case, read the `LAST SEEN` age or use `kubectl events -n demo`, which understands both formats.

## Current versus previous container logs

`kubectl logs` reads the log of a container in a Pod. Which instance it reads matters during a restart loop:

```
kubectl logs web-7d9c8b6f5-abcde -n demo
kubectl logs web-7d9c8b6f5-abcde -n demo --previous
kubectl logs web-7d9c8b6f5-abcde -n demo -c web --previous
```

- Without `--previous`, the command reads the current instance. In CrashLoopBackOff that instance may have just started or may not be running at all, so the output can be empty or cut short.
- With `--previous`, it reads the instance that last terminated. This holds the error that caused the exit and is usually the log to read first.
- `-c` selects a container in a multi-container Pod. Init containers are selected the same way by name.

The kubelet keeps only the current and one previous instance. Older crash logs are gone unless a log collector shipped them elsewhere. Logs also disappear when the Pod is deleted or replaced by a new rollout, so read them before taking remediation steps.

## Limiting log output

Long logs hide the relevant lines. Limit the output to the part around the failure:

```
kubectl logs web-7d9c8b6f5-abcde -n demo --previous --tail=100
kubectl logs web-7d9c8b6f5-abcde -n demo --since=15m --timestamps
kubectl logs deployment/web -n demo --tail=50
```

- `--tail=<n>` returns the last n lines. For a crash, the cause is usually in the last few dozen lines.
- `--since=<duration>` returns lines from a recent window, which helps align logs with event timestamps.
- `--timestamps` prefixes each line with the time the runtime received it, useful when the application does not log times itself.
- `deployment/web` reads from one Pod of the Deployment, chosen by kubectl. For a specific failing Pod, name the Pod.

## Collapsing repeated log lines

A crashing or retrying application often writes the same line many times, such as a connection retry or a health check entry. Repetition hides the one distinct line that explains the failure. When reviewing a log:

- Group identical lines and note how many times each appears, keeping the first and last occurrence times. A line repeated 300 times is one fact, not 300.
- Strip timestamps and request IDs before comparing, since they make otherwise identical lines look different.
- Pay most attention to lines that appear once, especially near the end of the previous instance's log. A single `invalid value` or `panic` line after a block of routine output is usually the cause.
- Keep the original order when quoting evidence, so the sequence of events stays visible.

On a workstation, `sort | uniq -c | sort -rn` over saved log output gives a quick frequency view.
