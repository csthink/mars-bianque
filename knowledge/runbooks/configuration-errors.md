# Configuration Errors at Start-up

Many applications validate their configuration when they start and exit immediately if a value is missing or malformed. In Kubernetes this shows up as a container that exits with code 1 within seconds of starting and then loops in CrashLoopBackOff, usually right after a change to environment variables, a ConfigMap, or a Secret.

## How configuration reaches the container

A container can receive configuration from several places in the Pod template, and a fault can sit in any of them:

- `env` entries with a literal `value`.
- `env` entries with `valueFrom.configMapKeyRef` or `valueFrom.secretKeyRef`, which copy one key from a ConfigMap or Secret.
- `envFrom`, which imports every key of a ConfigMap or Secret as environment variables.
- Volumes that mount a ConfigMap or Secret as files.
- `args` and `command`, which pass flags directly.

Environment variables are read once when the container starts. Changing a ConfigMap does not change the environment of running containers; the new value takes effect only after the container restarts or a new rollout replaces the Pods. Mounted files, by contrast, are refreshed in place after a delay, unless they are mounted with `subPath`.

This means a bad value can sit unnoticed in a ConfigMap until the next restart, and then every Pod fails at once.

## Recognizing a configuration failure

Configuration failures have a recognizable pattern:

- Exit code 1, sometimes 2 for rejected command-line flags.
- `Started` and `Finished` in the last termination state only a second or two apart.
- A short previous log that ends with a validation message rather than a stack trace from request handling.

Read the previous log of the crashed container:

```
kubectl logs web-7d9c8b6f5-abcde -n demo --previous --tail=50
```

Messages that point to configuration usually name the setting, for example:

- `invalid value "abc" for LISTEN_PORT: must be an integer`
- `required environment variable DATABASE_URL is not set`
- `unknown log level "verbose"`
- `open /etc/web/config.yaml: no such file or directory`

Write down the exact variable or key name and the rejected value from the log. The next step is to find where that value comes from.

## Missing ConfigMap or Secret keys

A reference to a ConfigMap or Secret that does not exist, or to a key that is not in it, fails before the application even runs. The kubelet cannot build the container's environment, so the container never starts:

- The Pod shows `CreateContainerConfigError` instead of CrashLoopBackOff.
- `kubectl describe pod` lists an event such as `Error: configmap "web-config" not found` or `Error: couldn't find key LOG_LEVEL in ConfigMap demo/web-config`.

Check the referenced objects and their keys:

```
kubectl get configmap web-config -n demo -o yaml
kubectl get secret web-credentials -n demo -o jsonpath='{.data}'
kubectl describe pod web-7d9c8b6f5-abcde -n demo
```

Key names are case-sensitive, and a key renamed in the ConfigMap without updating the Deployment breaks the reference. A reference marked `optional: true` does not block the start; the variable is just absent, and the application may then fail with a "not set" message instead.

Avoid printing decoded Secret values in tickets or chat; confirm that the key exists and compare lengths or formats instead.

## Comparing current values with the log message

When the application rejects a value, confirm that the Deployment actually supplies that value. Print the container's environment as defined in the Pod template:

```
kubectl get deployment web -n demo -o jsonpath='{range .spec.template.spec.containers[*].env[*]}{.name}={.value}{"\n"}{end}'
kubectl get deployment web -n demo -o jsonpath='{.spec.template.spec.containers[*].envFrom}'
```

Entries that come from `valueFrom` print an empty value in the first command; follow their reference to the ConfigMap or Secret.

Then compare:

- If the log says `LISTEN_PORT` is `abc` and the Deployment sets `LISTEN_PORT=abc`, the fault is in the Pod template.
- If the Deployment uses `configMapKeyRef`, read the ConfigMap key and compare there.
- If the value looks right in the Deployment, check for duplicates: a later `env` entry overrides an earlier one, and an explicit `env` entry overrides the same name imported by `envFrom`.

Watch for invisible differences such as trailing whitespace, quotes stored as part of the value, or a number written where a duration with units is expected.

## Finding the change that introduced the value

A configuration fault almost always arrives with a change. Find which one:

```
kubectl rollout history deployment/web -n demo
kubectl rollout history deployment/web -n demo --revision=6
kubectl rollout history deployment/web -n demo --revision=7
```

Compare the `Environment` section of the two most recent revisions. If the bad value appears only in the newer one, and the first crash follows that rollout, the rollout introduced it.

A ConfigMap edit does not create a Deployment revision. If the Pod template is unchanged, check when the ConfigMap changed. Its `metadata.managedFields` timestamps show the last update time, and in a GitOps setup the commit log of the ConfigMap manifest shows who changed which key:

```
git log --oneline -- deploy/web/configmap.yaml
git show <commit> -- deploy/web/configmap.yaml
```

Line up the change time with the first restart in the namespace events.

## Remediation: restoring a valid value

Every action here changes the cluster or the manifest repository and needs human confirmation before it is applied.

- If a Deployment rollout introduced the value, revert it. With GitOps, `git revert <commit>` and `git push`, then let the sync roll the Pods. Without GitOps, `kubectl rollout undo deployment/web -n demo`.
- If a ConfigMap change caused it, revert the ConfigMap change in its source. Because environment variables are read only at start, the Pods recover on their next restart, which in a crash loop happens on its own.
- Fix the value in the manifest source instead of using `kubectl edit` or `kubectl set env`, so the next sync does not reintroduce the fault.
