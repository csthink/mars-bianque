---
name: diagnose-probe-failure
description: Diagnose a container that the kubelet restarts because its liveness probe fails (Unhealthy events, Killing events, exit code 137 or 143 without OOMKilled). Use it when the events show probe failures.
---

# Diagnose a failing liveness probe

Use this skill when the events show that a probe fails and the container is killed for it.
The root cause category is `probe_misconfig` when the probe asks for something the
application does not serve (a wrong path or port, a timing that start-up cannot meet).

## Evidence to collect

| Domain | Tool | Arguments | Expected evidence |
| --- | --- | --- | --- |
| kubernetes | `k8s_list_events` | namespace (optionally the pod as involved object) | Unhealthy events with the probe kind and the failure, such as an HTTP status code, and Killing events |
| kubernetes | `k8s_get_deployment` | namespace, workload name | The probe of the container: path, port, initial delay, period and failure threshold |
| kubernetes | `k8s_get_pod_status` | namespace, pod | Last termination with exit code 137 or 143 and a reason other than `OOMKilled` |
| logs | `logs_get_container_logs` | namespace, pod, container, `previous: false` | Whether the application starts and which requests it answers |

A probe misconfiguration is established when the probe failure in the events matches the
probe settings of the deployment, for example a 404 for the configured path while the
logs show the application running.

## Root cause object

The object is the Deployment that owns the pod. The field is the container's probe, for
example `spec.template.spec.containers[<container>].livenessProbe`.

## Change attribution

Follow attribute-gitops-change: find the commit whose diff changed the probe, and match its
time with the first Unhealthy events.

## Background

Runbook topic: Liveness, Readiness, and Startup Probes.
