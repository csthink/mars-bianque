---
name: diagnose-config-error
description: Diagnose a container that exits at start because of an invalid or missing configuration value (an environment variable, a ConfigMap or Secret key, a command-line flag). Use it when the previous container log names a setting and the exit code is not 137.
---

# Diagnose a configuration error

Use this skill when the container exits soon after it starts, the last termination is not
`OOMKilled`, and the logs point at a setting. The root cause category is `config_error`.

## Evidence to collect

| Domain | Tool | Arguments | Expected evidence |
| --- | --- | --- | --- |
| kubernetes | `k8s_get_pod_status` | namespace, pod | Last termination with a non-zero exit code (often 1) and a short running time |
| logs | `logs_get_container_logs` | namespace, pod, container, `previous: true` | The line that names the setting and why its value is rejected |
| kubernetes | `k8s_get_deployment` | namespace, workload name | The current value of that setting in the container's environment or arguments |

The configuration error is established when the log names a setting and the deployment
shows the value the log rejects. Quote both by their evidence IDs.

## Root cause object

The object is the Deployment that owns the pod. The field is the container's environment
variable or argument, for example `spec.template.spec.containers[app].env[APP_MODE]`.

## Change attribution

A configuration value comes from the manifests, so the change domain is required: follow
attribute-gitops-change to find the commit or revision that set the value. When several
commits touched the manifest, the one whose diff sets the rejected value is the cause, not
the newest one.

## When evidence is missing

Without the previous container log the setting cannot be named from the logs: say so in
the missing evidence, keep the confidence below high, and rely on the deployment and the
change diff.

## Background

Runbook topic: Configuration Errors at Start-up.
