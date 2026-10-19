"""Evidence tool of the ``logs`` domain: container logs, cut to a size the model can read."""

from __future__ import annotations

from pydantic import Field

from bianque.tools.backends.base import ToolBackends
from bianque.tools.fields import ContainerName, Namespace, ObjectName, ToolArgs
from bianque.tools.output import ToolOutput, collect_identifiers, render
from bianque.tools.registry import tool

#: Upper bound and default of the lines a log result keeps (after folding repeats).
LOG_MAX_LINES = 200

#: Longer lines are cut to this many characters.
LINE_MAX_CHARS = 1000


def fold_and_truncate(text: str, max_lines: int) -> tuple[list[str], int, bool]:
    """Fold consecutive repeated lines, keep the last ``max_lines`` and cut long lines.

    A run of identical lines becomes one line followed by ``[repeated N times]``, so
    the count survives. Truncation keeps the end of the log, where a crash is reported.

    Args:
        text: The raw log.
        max_lines: How many lines to keep after folding.

    Returns:
        The kept lines, the number of raw lines, and whether lines were dropped.
    """
    raw = text.splitlines()
    folded: list[str] = []
    previous: str | None = None
    repeats = 0

    def flush() -> None:
        if previous is None:
            return
        line = previous
        if len(line) > LINE_MAX_CHARS:
            line = line[:LINE_MAX_CHARS] + " [line truncated]"
        folded.append(line if repeats == 1 else f"{line} [repeated {repeats} times]")

    for line in raw:
        if line == previous:
            repeats += 1
            continue
        flush()
        previous, repeats = line, 1
    flush()
    return folded[-max_lines:], len(raw), len(folded) > max_lines


class ContainerLogsArgs(ToolArgs):
    """Arguments of ``logs_get_container_logs``."""

    namespace: Namespace
    pod: ObjectName = Field(description="Pod name.")
    container: ContainerName
    previous: bool = Field(
        default=False, description="Read the previous, terminated instance of the container."
    )
    tail_lines: int = Field(
        default=LOG_MAX_LINES,
        ge=1,
        le=LOG_MAX_LINES,
        description="How many of the last lines to return, after folding repeated lines.",
    )


@tool(
    name="logs_get_container_logs",
    kind="evidence",
    domain="logs",
    source="kubernetes",
    read_only=True,
    timeout_s=10,
    args=ContainerLogsArgs,
    identity_args=("namespace", "pod", "container", "previous"),
)
async def logs_get_container_logs(args: ContainerLogsArgs, backends: ToolBackends) -> ToolOutput:
    """Read the log of a container, current or previous instance.

    Consecutive repeated lines are folded with a count and only the last lines are
    kept; the result says how many lines the log had and whether lines were dropped.
    """
    text = await backends.kubernetes.read_log(
        args.namespace, args.pod, args.container, args.previous
    )
    lines, total, truncated = fold_and_truncate(text, args.tail_lines)
    data = {
        "namespace": args.namespace,
        "pod": args.pod,
        "container": args.container,
        "previous": args.previous,
        "total_lines": total,
        "truncated": truncated,
        "lines": lines,
    }
    return ToolOutput(render(data), collect_identifiers([args.namespace, args.pod, args.container]))
