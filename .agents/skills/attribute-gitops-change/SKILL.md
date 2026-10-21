---
name: attribute-gitops-change
description: Find the change that introduced a faulty value in a workload's specification, from the rollout history of the deployment and the commits of the manifest repository. Use it in a supplementary plan once the first round points at a configuration, resource limit, probe or image problem.
---

# Attribute a fault to a GitOps change

Use this skill once the current state points at a value in the workload's specification
(`config_error`, `resource_limit`, `probe_misconfig` or `image_error`). The manifests are
kept in a Git repository and applied from it, so the change that introduced the value is
a commit, and the deployment shows it as a new revision.

## Evidence to collect

Plan these steps in the `change` domain; the first round may not use it.

| Tool | Arguments | Expected evidence |
| --- | --- | --- |
| `change_get_rollout_history` | namespace, deployment | Revisions with their times, and the pod template difference between the last two |
| `change_list_commits` | optionally the path of the deployment manifest | Recent commits with author, time, message and changed files |
| `change_show_commit` | the commit that changed the deployment manifest | The diff of that commit |

## Choosing the commit

- Prefer commits that touch the deployment manifest; ignore commits that change only
  documentation or other workloads.
- When several commits touch the manifest, choose the one whose diff sets the faulty value
  found in the first round, not simply the newest.
- Check that the commit time, the new revision and the first restarts or failing events
  line up.

## What establishes the root cause

The root cause cites the diff that introduces the faulty value (from `change_show_commit`,
or the template difference from `change_get_rollout_history`) together with the evidence
of the failure it causes (pod status, events or logs). Without change evidence the
category can be named but the change cannot; say that the change is missing.

## Background

Runbook topics: Attributing a Fault to a GitOps Commit; Deployment Rollout History.
