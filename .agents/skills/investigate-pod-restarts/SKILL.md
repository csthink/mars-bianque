---
name: investigate-pod-restarts
description: Plan the investigation of an alert about a pod whose container keeps restarting (a rising restart count, CrashLoopBackOff). Read the termination state and the logs first, then follow the diagnosis skill that matches what the evidence shows.
---

# Investigate pod restarts

Use this skill first for an alert that names a pod whose container restarts, such as a
restart-count alert or a CrashLoopBackOff alert. It decides what the first round collects
and which diagnosis skill to follow next.

## First round: the current state

The first round reads only the `kubernetes` and `logs` domains. Plan one step per question:

| Domain | Tool | Arguments from the alert | Expected evidence |
| --- | --- | --- | --- |
| kubernetes | `k8s_get_pod_status` | namespace, pod | Container state, restart count, and the reason and exit code of the last termination |
| kubernetes | `k8s_list_events` | namespace (optionally the pod as involved object) | BackOff, Unhealthy (probe), Killing, Failed or FailedScheduling events with counts and times |
| kubernetes | `k8s_get_deployment` | namespace, the workload name (the pod name without its ReplicaSet and pod suffixes) | Image, environment variables, resource limits and probes of the containers |
| logs | `logs_get_container_logs` | namespace, pod, container, `previous: true` | The last lines the terminated container wrote before it exited |
| logs | `logs_get_container_logs` | namespace, pod, container, `previous: false` | What the current instance writes while it starts |

Use `k8s_list_pods` when the alert names no pod, or to see whether other pods of the
workload restart too.

## Branch on the evidence

Name the hypothesis the first-round evidence supports, then follow its skill:

| Evidence | Root cause category | Next skill |
| --- | --- | --- |
| Last termination reason `OOMKilled`, exit code 137 | `resource_limit` | diagnose-oomkilled |
| Unhealthy events from a failing liveness probe, `Killing` events, exit code 137 or 143 without `OOMKilled` | `probe_misconfig` | diagnose-probe-failure |
| Exit code 1 (or another non-zero code) and a log line naming an invalid or missing setting | `config_error` | diagnose-config-error |
| Waiting reason `ImagePullBackOff` or `ErrImagePull` | `image_error` | none; check the image reference in the deployment |
| None of the above, or the pod status is missing | none yet | keep the root cause unknown unless more evidence settles it |

## When the change domain is needed

The `change` domain is planned in a supplementary plan, not in the first round. For
`config_error`, `resource_limit`, `probe_misconfig` and `image_error` the investigation is
not complete until the change that introduced the faulty value is found: follow
attribute-gitops-change.

## Background

Runbook topics: CrashLoopBackOff; Reading Events and Container Logs.
