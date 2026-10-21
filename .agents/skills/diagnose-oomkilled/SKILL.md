---
name: diagnose-oomkilled
description: Diagnose a container killed for exceeding its memory limit (last termination reason OOMKilled, exit code 137). Use it when the pod status shows OOMKilled.
---

# Diagnose an OOMKilled container

Use this skill when the last termination reason of the container is `OOMKilled`. The root
cause category is `resource_limit`.

## Evidence to collect

| Domain | Tool | Arguments | Expected evidence |
| --- | --- | --- | --- |
| kubernetes | `k8s_get_pod_status` | namespace, pod | Last termination reason `OOMKilled`, exit code 137, restart count |
| kubernetes | `k8s_get_deployment` | namespace, workload name | The memory request and limit of the container |
| logs | `logs_get_container_logs` | namespace, pod, container, `previous: true` | Optional: what the container did before the kill; an OOM kill often leaves no message |

The pod status alone shows that the limit was hit. Whether the limit is too low or the
workload uses more memory than before is decided by the change history, not by the logs.

## Root cause object

The object is the Deployment that owns the pod. The field is the container's memory limit,
`spec.template.spec.containers[<container>].resources.limits.memory`.

## Change attribution

Follow attribute-gitops-change. For a limit, the rollout history is the most direct
evidence: `change_get_rollout_history` returns the pod template difference between the last
two revisions, which shows the old and the new limit. A commit diff from
`change_show_commit` that lowers the limit confirms which change introduced it.

## Background

Runbook topic: OOMKilled and Exit Code 137.
