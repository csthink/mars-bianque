"""Evidence tools of the ``change`` domain: what changed and when.

The deployment's rollout history comes from its replica sets; the commit history comes
from the manifest repository. The two corroborate each other: a new revision appears
when a commit that changed the pod template was applied.
"""

from __future__ import annotations

import difflib
import json
from typing import Annotated, Any

from pydantic import Field

from bianque.tools.backends.base import KubeObject, ToolBackends
from bianque.tools.errors import ToolError
from bianque.tools.fields import Namespace, ObjectName, ToolArgs, format_time, metadata
from bianque.tools.output import ToolOutput, collect_identifiers, render
from bianque.tools.registry import tool

#: Length of the abbreviated commit hash shown in lists.
SHORT_HASH = 12

#: Diffs longer than this many lines are cut.
MAX_DIFF_LINES = 400

_REVISION = "deployment.kubernetes.io/revision"
_HASH_LABEL = "pod-template-hash"
_TIMEOUT_S = 10


def _revision(rs: KubeObject) -> int:
    try:
        return int((metadata(rs).get("annotations") or {}).get(_REVISION, 0))
    except ValueError:
        return 0


def _template_text(rs: KubeObject) -> list[str]:
    template = json.loads(json.dumps((rs.get("spec") or {}).get("template") or {}))
    labels = (template.get("metadata") or {}).get("labels") or {}
    labels.pop(_HASH_LABEL, None)
    return json.dumps(template, indent=2, sort_keys=True).splitlines()


def _cut(lines: list[str]) -> tuple[str, bool]:
    if len(lines) <= MAX_DIFF_LINES:
        return "\n".join(lines), False
    return "\n".join(lines[:MAX_DIFF_LINES]), True


class RolloutHistoryArgs(ToolArgs):
    """Arguments of ``change_get_rollout_history``."""

    namespace: Namespace
    deployment: ObjectName = Field(description="Deployment name.")


@tool(
    name="change_get_rollout_history",
    kind="evidence",
    domain="change",
    source="kubernetes",
    read_only=True,
    timeout_s=_TIMEOUT_S,
    args=RolloutHistoryArgs,
    identity_args=("namespace", "deployment"),
)
async def change_get_rollout_history(
    args: RolloutHistoryArgs, backends: ToolBackends
) -> ToolOutput:
    """Read a deployment's revision history and the pod template diff of its last revisions.

    Each revision is a replica set with its revision number, creation time and replicas;
    the diff compares the pod templates of the two newest revisions.
    """
    owned = [
        rs
        for rs in await backends.kubernetes.list_replicasets(args.namespace)
        if any(
            ref.get("kind") == "Deployment" and ref.get("name") == args.deployment
            for ref in metadata(rs).get("ownerReferences") or []
        )
    ]
    if not owned:
        raise ToolError("not_found", f"rollout history {args.namespace}/{args.deployment}")
    owned.sort(key=lambda rs: (_revision(rs), str(metadata(rs).get("name"))))
    revisions = [
        {
            "revision": _revision(rs),
            "replicaset": metadata(rs).get("name"),
            "created_at": format_time(metadata(rs).get("creationTimestamp")),
            "replicas": (rs.get("spec") or {}).get("replicas", 0),
            "ready": (rs.get("status") or {}).get("readyReplicas", 0),
        }
        for rs in owned
    ]
    diff: dict[str, Any] | None = None
    if len(owned) >= 2:
        older, newer = owned[-2], owned[-1]
        lines = list(
            difflib.unified_diff(
                _template_text(older),
                _template_text(newer),
                fromfile=f"revision {_revision(older)}",
                tofile=f"revision {_revision(newer)}",
                lineterm="",
            )
        )
        patch, truncated = _cut(lines)
        diff = {
            "from_revision": _revision(older),
            "to_revision": _revision(newer),
            "patch": patch,
            "truncated": truncated,
        }
    data = {
        "namespace": args.namespace,
        "deployment": args.deployment,
        "revisions": revisions,
        "template_diff": diff,
    }
    identifiers = collect_identifiers(
        [args.namespace, args.deployment], [str(row["replicaset"]) for row in revisions]
    )
    return ToolOutput(render(data), identifiers)


#: A path inside the repository; it may not start with a dash, so it is never an option.
RepoPath = Annotated[str, Field(max_length=256, pattern=r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$")]

_FIELD = "\x1f"
_RECORD = "\x1e"


class ListCommitsArgs(ToolArgs):
    """Arguments of ``change_list_commits``."""

    path: RepoPath | None = Field(default=None, description="Only commits touching this path.")
    max_count: int = Field(default=20, ge=1, le=50, description="Newest commits to return.")


@tool(
    name="change_list_commits",
    kind="evidence",
    domain="change",
    source="git",
    read_only=True,
    timeout_s=_TIMEOUT_S,
    args=ListCommitsArgs,
    identity_args=("path",),
)
async def change_list_commits(args: ListCommitsArgs, backends: ToolBackends) -> ToolOutput:
    """List the newest commits of the manifest repository, newest first.

    Each commit has its abbreviated hash, author, date, subject and changed files.
    """
    log_args = [
        f"--max-count={args.max_count}",
        f"--format={_RECORD}%H{_FIELD}%an{_FIELD}%aI{_FIELD}%s",
        "--name-only",
        "HEAD",
    ]
    if args.path:
        log_args += ["--", args.path]
    output = await backends.git.run("log", log_args)
    commits = []
    hashes = []
    for record in output.split(_RECORD)[1:]:
        header, _, rest = record.partition("\n")
        full, author, date, subject = (header.split(_FIELD) + ["", "", "", ""])[:4]
        hashes.append(full[:SHORT_HASH])
        commits.append(
            {
                "commit": full[:SHORT_HASH],
                "author": author,
                "date": format_time(date),
                "subject": subject,
                "files": [line for line in rest.splitlines() if line.strip()],
            }
        )
    data = {"path": args.path, "commits": commits}
    return ToolOutput(render(data), collect_identifiers(hashes))


class ShowCommitArgs(ToolArgs):
    """Arguments of ``change_show_commit``."""

    commit: str = Field(
        pattern=r"^[0-9a-f]{7,40}$", description="Commit hash, abbreviated or full."
    )


@tool(
    name="change_show_commit",
    kind="evidence",
    domain="change",
    source="git",
    read_only=True,
    timeout_s=_TIMEOUT_S,
    args=ShowCommitArgs,
    identity_args=("commit",),
)
async def change_show_commit(args: ShowCommitArgs, backends: ToolBackends) -> ToolOutput:
    """Show one commit of the manifest repository: author, date, message and diff."""
    try:
        full = await backends.git.run("rev-parse", ["--verify", f"{args.commit}^{{commit}}"])
    except ToolError as exc:
        raise ToolError("not_found", f"commit {args.commit}", exc.detail) from exc
    full = full.strip()
    header = await backends.git.run(
        "show", ["--no-patch", f"--format=%an{_FIELD}%aI{_FIELD}%B", full]
    )
    author, date, message = (header.split(_FIELD, 2) + ["", "", ""])[:3]
    patch = await backends.git.run("show", ["--format=", "--patch", "--no-color", full])
    diff, truncated = _cut(patch.strip("\n").splitlines())
    data = {
        "commit": full,
        "short": full[:SHORT_HASH],
        "author": author,
        "date": format_time(date),
        "message": message.strip(),
        "diff": diff,
        "diff_truncated": truncated,
    }
    return ToolOutput(render(data), collect_identifiers([full, full[:SHORT_HASH]]))
