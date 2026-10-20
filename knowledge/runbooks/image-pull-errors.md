# ImagePullBackOff and ErrImagePull

When the kubelet cannot download a container image, the Pod never starts its container and reports `ErrImagePull`, then `ImagePullBackOff`. The cause is almost always one of three things: the image reference is wrong, the registry rejects the credentials, or the node cannot reach the registry.

## How the two statuses relate

`ErrImagePull` is reported when a pull attempt fails. The kubelet then retries with an increasing delay, capped at five minutes, and while it waits the container shows `ImagePullBackOff`. A Pod therefore alternates between the two statuses for as long as the problem lasts.

Points to keep in mind:

- The container has never run, so there are no container logs. `kubectl logs` returns an error saying the container is waiting to start. All evidence comes from Pod status and events.
- The RESTARTS count stays at 0, because nothing was started. An alert about restarts does not match this failure; an alert about Pods not becoming ready does.
- During a rolling update, old Pods keep serving traffic while new Pods are stuck, so the service may still be up but the rollout never completes.
- `imagePullPolicy` affects when pulls happen. With `IfNotPresent`, a node that already has the image cached starts the Pod even if the registry is unreachable, which can make the failure appear on some nodes only.

## Reading the pull events

The events on the Pod contain the registry's actual answer, which identifies the cause:

```
kubectl describe pod web-6f8d7c9b4-xyz12 -n demo
kubectl get events -n demo --field-selector involvedObject.name=web-6f8d7c9b4-xyz12
```

The sequence normally reads `Pulling`, then `Failed` with the error text, then `BackOff`. The `Failed` message is the one to read closely:

- `manifest unknown` or `not found`: the repository exists but the tag does not.
- `repository does not exist or may require 'docker login'`: wrong repository name, or the registry hides private repositories from unauthenticated clients.
- `401 Unauthorized` or `403 Forbidden`: credentials are missing, wrong, or lack read permission.
- `no such host` or `i/o timeout`: the node cannot resolve or reach the registry.
- `toomanyrequests`: the registry is rate-limiting the node's pulls.
- `no matching manifest for linux/arm64`: the image was not built for the node's CPU architecture.

## Wrong image name or tag

A typo in the image reference is the most common cause, often introduced by a release that bumps the tag. Read the reference exactly as the Pod uses it:

```
kubectl get deployment web -n demo -o jsonpath='{.spec.template.spec.containers[*].image}'
kubectl get pod web-6f8d7c9b4-xyz12 -n demo -o jsonpath='{.spec.containers[*].image}'
```

Check each part of `registry.example.com/team/web:1.4.2`:

- **Registry host**: a missing host defaults to Docker Hub, so `team/web:1.4.2` is looked up in the wrong place.
- **Repository path**: case and separators must match exactly.
- **Tag**: a tag that was never pushed, was pushed under a different version, or was deleted by a retention policy returns `manifest unknown`.
- **Digest**: a reference pinned with `@sha256:` must match a manifest that still exists.

Compare the image with the previous Deployment revision using `kubectl rollout history deployment/web -n demo --revision=<n>`. If only the tag changed and the new tag fails, confirm in the registry or the build pipeline that the tag was actually published.

## Missing or wrong registry credentials

Private registries require credentials, supplied through `imagePullSecrets` on the Pod or on its ServiceAccount. Check each link of that chain:

```
kubectl get pod web-6f8d7c9b4-xyz12 -n demo -o jsonpath='{.spec.imagePullSecrets}'
kubectl get serviceaccount default -n demo -o jsonpath='{.imagePullSecrets}'
kubectl get secret registry-pull -n demo
```

Common faults:

- The Pod template has no `imagePullSecrets`, and the ServiceAccount does not provide one either.
- The referenced Secret does not exist in the Pod's namespace. Secrets are namespaced; one created in another namespace does not count.
- The Secret has the wrong type. It must be `kubernetes.io/dockerconfigjson` with a `.dockerconfigjson` key.
- The Secret's entry is for a different registry host than the one in the image reference.
- The credentials expired or were rotated in the registry but not in the cluster.

When a referenced pull Secret is missing, the kubelet may also emit a warning that it could not retrieve the pull secret. Do not print decoded credentials while checking; confirm the registry host inside the Secret and its type instead.

## Remediation: fixing the image reference or credentials

These changes modify cluster objects or the manifest source, and each one needs human confirmation before it is applied.

- If a recent change introduced a bad tag or repository, revert it. With GitOps, `git revert <commit>` and `git push`; otherwise `kubectl rollout undo deployment/web -n demo`. The old ReplicaSet scales back up with the image that worked.
- If the tag is correct but was never published, fix the build pipeline rather than the Deployment.
- If credentials are missing, create or update the pull Secret through the normal secret management process and reference it in the manifest, then apply with `kubectl apply -f`. Never commit registry credentials to the manifest repository in plain text.
