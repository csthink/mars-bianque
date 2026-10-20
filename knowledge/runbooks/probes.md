# Liveness, Readiness, and Startup Probes

Probes are periodic health checks the kubelet runs against a container. A misconfigured probe can restart a healthy application or keep it out of service, so probe failures belong on the checklist for any restart or unavailability alert.

## What each probe controls

The three probe types have different consequences when they fail:

- **Liveness probe**: answers "should this container be restarted?" After `failureThreshold` consecutive failures, the kubelet kills the container and starts it again according to the restart policy.
- **Readiness probe**: answers "should this Pod receive traffic?" A failing readiness probe removes the Pod's address from Service endpoints. The container keeps running and is not restarted.
- **Startup probe**: answers "has the application finished starting?" While it has not yet succeeded, liveness and readiness checks are held back. If it never succeeds within `failureThreshold × periodSeconds`, the container is killed and restarted.

Each probe can use an HTTP GET (success is a status code from 200 to 399), a TCP connection, a gRPC health check, or a command run inside the container (success is exit code 0).

Only the liveness and startup probes cause restarts. If an alert reports restarts, a readiness probe is not the cause, although it may fail for the same underlying reason.

## Recognizing probe failure events

The kubelet reports each failed check as a `Warning` event with reason `Unhealthy` on the Pod:

```
kubectl get events -n demo --field-selector involvedObject.name=web-7d9c8b6f5-abcde
kubectl describe pod web-7d9c8b6f5-abcde -n demo
```

Typical messages:

- `Liveness probe failed: HTTP probe failed with statuscode: 404`
- `Liveness probe failed: Get "http://192.0.2.10:8080/healthz": dial tcp 192.0.2.10:8080: connect: connection refused`
- `Readiness probe failed: Get "http://192.0.2.10:8080/ready": context deadline exceeded`
- `Startup probe failed: ...`

After liveness failures reach the threshold, an event with reason `Killing` follows, saying the container failed its liveness probe and will be restarted. The terminated container usually shows exit code 143 (SIGTERM) or, if it ignored SIGTERM until the grace period ended, 137.

The event `count` and the gap between first and last timestamps show whether failures are constant or intermittent.

## Wrong path or port

A probe that targets the wrong endpoint fails on every attempt, no matter how healthy the application is. Check the probe definition against what the application actually serves:

```
kubectl get deployment web -n demo -o jsonpath='{.spec.template.spec.containers[*].livenessProbe}'
kubectl get deployment web -n demo -o jsonpath='{.spec.template.spec.containers[*].ports}'
```

How the failure message maps to the mistake:

- `statuscode: 404` means the port is right but the path is wrong, for example `/health` configured while the application serves `/healthz`.
- `connection refused` means nothing listens on that port, either because the port number is wrong or the application has not opened its listener yet.
- `statuscode: 401` or `403` means the health endpoint requires authentication; probes should target an unauthenticated path.
- A named port such as `port: http` must match a `name` in the container's `ports` list.

Also confirm the scheme. A probe using `scheme: HTTP` against a listener that only accepts TLS fails, and the reverse fails too.

## Timing parameters and slow start-up

When the endpoint is correct but the application needs time to start, timing settings decide whether it survives:

- `initialDelaySeconds`: wait before the first check. Default 0.
- `periodSeconds`: interval between checks. Default 10.
- `timeoutSeconds`: how long one check may take. Default 1, which is short for endpoints that touch a database.
- `failureThreshold`: consecutive failures before action. Default 3.

The time a container has before its first liveness restart is roughly `initialDelaySeconds + failureThreshold × periodSeconds`. If the application needs 60 seconds to load data and the liveness probe allows only 30, every start ends in a kill, and the Pod loops without ever logging an application error.

`context deadline exceeded` or `Client.Timeout exceeded` in the event message points to `timeoutSeconds` being too small or the endpoint being slow under load.

For slow-starting applications, a startup probe with a generous `failureThreshold` is the cleaner fix than a large `initialDelaySeconds` on the liveness probe.

## How a bad liveness probe causes restart loops

A liveness probe that can never pass turns a healthy container into a restart loop. The sequence is:

1. The container starts and the application works normally.
2. The liveness probe fails because of a wrong path, port, or timeout.
3. After the failure threshold, the kubelet sends SIGTERM and records a `Killing` event.
4. The container restarts, and the cycle repeats with growing backoff until the Pod shows CrashLoopBackOff.

Distinguishing features compared with an application crash:

- The previous container log shows normal start-up and request handling, then a shutdown triggered by SIGTERM, with no error from the application itself.
- The exit code is 143 or 137, not 1.
- `Unhealthy` events appear just before each restart.
- The time between restarts is nearly constant at the probe's failure window.

If the probe definition changed in a recent revision, compare it with the previous revision using `kubectl rollout history --revision`.

## Remediation: correcting a probe

Probe changes modify the Deployment and trigger a rollout, so they need human confirmation before they are applied.

- If a recent change introduced the wrong path, port, or timing, revert that change in the manifest source. With GitOps, use `git revert <commit>` and `git push`; otherwise use `kubectl rollout undo deployment/web -n demo`.
- If the probe was always wrong, correct the path or port in the manifest and apply it with `kubectl apply -f deployment.yaml` through the normal review path.
- Prefer adding a startup probe over removing the liveness probe. Removing liveness checks hides real hangs.
