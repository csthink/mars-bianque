# CrashLoopBackOff

A Pod in CrashLoopBackOff has a container that starts, exits, and is started again by the kubelet with a growing delay between attempts. The status is a symptom, not a cause: the cause is whatever makes the container process exit, and the exit code plus the log of the previous attempt usually identify it.

## What CrashLoopBackOff means

CrashLoopBackOff is a waiting reason, not a terminal state. The container ran, the process ended, and the Pod's `restartPolicy` (normally `Always` for Deployments) tells the kubelet to start it again. Between attempts the container sits in `Waiting` with reason `CrashLoopBackOff`.

The process may exit with a non-zero code (a crash or a deliberate exit after a start-up check) or with code 0. A clean exit still loops under `restartPolicy: Always`, because a Deployment container is expected to run forever. A container killed by the kubelet after failed liveness probes also restarts and can end up in the same state.

Signals that point to this runbook:

- `kubectl get pods` shows `CrashLoopBackOff` or `Error` in the STATUS column and a RESTARTS count that keeps rising.
- Events contain `Back-off restarting failed container`.
- An alert reports that a container restarted several times in a short window.

## Restart backoff timing

The kubelet does not restart a failed container immediately every time. After each exit it waits longer before the next attempt: the delay starts at about ten seconds and doubles on each consecutive failure up to a cap of five minutes. If a container then runs successfully for ten minutes, the delay resets.

Practical consequences for diagnosis:

- Early in an incident, restarts come quickly, so the RESTARTS count climbs fast in the first few minutes and then slows down.
- During the backoff window the container is not running, so `kubectl logs` without `--previous` may return nothing or an error. Read the previous attempt instead.
- A Pod that alternates between `Running` and `CrashLoopBackOff` is usually failing a few seconds after start, often right after reading configuration or connecting to a dependency.
- Fixing the cause does not cut the current wait short; the next attempt still happens when the backoff timer expires, unless the Pod is replaced by a new rollout.

## Collecting the termination state

Start with the Pod's last termination record. It holds the exit code, the reason, and the start and finish times of the attempt that failed.

```
kubectl get pods -n demo
kubectl describe pod web-7d9c8b6f5-abcde -n demo
kubectl get pod web-7d9c8b6f5-abcde -n demo -o jsonpath='{.status.containerStatuses[*].lastState.terminated}'
```

In the `describe` output, look at the container section:

- `State: Waiting, Reason: CrashLoopBackOff` confirms the loop.
- `Last State: Terminated` gives `Reason` (`Error`, `OOMKilled`, `Completed`), `Exit Code`, `Started`, and `Finished`.
- `Restart Count` shows how many times the loop has run.

A very short gap between `Started` and `Finished` means the process failed during initialization. A long gap followed by exit code 137 or 143 suggests the process was killed from outside, by the out-of-memory killer or by the kubelet after liveness probe failures. Check the Events section at the bottom of the same output for probe failures before assuming an application crash.

## Reading exit codes

Exit codes from 0 to 125 come from the process itself. Codes above 128 mean the process was terminated by a signal, and the signal number is the code minus 128.

- **1**: generic application error. The program caught a problem and exited, for example a failed configuration parse or a refused database connection. The previous log almost always says why.
- **2**: misuse of a shell built-in or an invalid command-line argument. Common after a change to `command` or `args` in the Pod template.
- **126**: the entrypoint was found but could not be executed, usually missing execute permission or a binary built for a different CPU architecture.
- **127**: command not found. The `command` path is wrong or the binary is not in the image.
- **137**: SIGKILL (128 + 9). Either the kernel out-of-memory killer (reason `OOMKilled`) or a forced kill after the termination grace period. See the OOMKilled runbook.
- **139**: SIGSEGV (128 + 11), a segmentation fault in native code or a corrupted binary.
- **143**: SIGTERM (128 + 15). The process was asked to stop, typically by the kubelet after a failed liveness probe or during a normal shutdown.

## Reading the previous container log

The log of the attempt that crashed is kept until the next attempt replaces it. Read it with `--previous`:

```
kubectl logs web-7d9c8b6f5-abcde -n demo --previous
kubectl logs web-7d9c8b6f5-abcde -n demo -c web --previous --tail=100
```

Use `-c` when the Pod has more than one container. If init containers are involved, check them too, because a failing init container keeps the Pod in `Init:CrashLoopBackOff` and the main container never starts.

What to look for in the last lines before exit:

- An explicit error about configuration, such as an invalid value, a missing key, or a file that does not exist.
- A connection error to a database, cache, or other dependency.
- A stack trace or panic message that names the failing function.
- No output at all, which suggests the process failed before logging was set up (exit 126 or 127) or was killed from outside (exit 137 or 143).

Compare the first timestamp of the error with the time the Deployment last changed; a match points to a recent rollout as the trigger.

## Narrowing down the cause

Once the exit code and previous log are known, match them to the likely cause and the runbook that covers it:

- Exit 1 with a message about an environment variable, flag, or ConfigMap key: configuration error. Compare the value in the Deployment with what the log rejects.
- Exit 137 with reason `OOMKilled`: memory limit too small or a memory leak.
- Exit 143 or 137 together with `Liveness probe failed` events: probe misconfiguration rather than an application crash.
- Exit 126, 127, or 2 right after a rollout: wrong `command`, `args`, or image contents.
- Exit 1 with connection errors only: a dependency is unavailable; check that service before changing this workload.

In every case, find out what changed. Use `kubectl rollout history` and, when manifests live in Git, the commit log of the manifest path, and line up the change time with the first restart.
