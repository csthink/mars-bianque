"""Auxiliary tools that read stored investigations, for MCP clients.

Both take a namespace: the executor checks it against the namespaces an MCP client may
read, as for every tool with a ``namespace`` argument, and the tools only return
investigations whose alert is in that namespace. An investigation of another namespace
reads as not found, so a client learns nothing about it. The results are reports and
summaries, never evidence: the executor stores no evidence for an auxiliary tool.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from bianque.tools.backends.base import (
    InvestigationRow,
    InvestigationSource,
    ToolBackends,
)
from bianque.tools.errors import ToolError
from bianque.tools.fields import Namespace, ToolArgs, format_time
from bianque.tools.output import ToolOutput, collect_identifiers, render
from bianque.tools.registry import tool

#: Upper bound and default of the investigations one listing returns.
LIST_MAX_LIMIT = 50
LIST_DEFAULT_LIMIT = 10

InvestigationId = Annotated[
    str,
    Field(pattern=r"^inv_[0-9A-HJKMNP-TV-Z]{26}$", description="Investigation ID, inv_<ULID>."),
]


class InvestigationListArgs(ToolArgs):
    """Arguments of ``investigation_list``."""

    namespace: Namespace
    limit: int = Field(
        default=LIST_DEFAULT_LIMIT,
        ge=1,
        le=LIST_MAX_LIMIT,
        description="How many investigations to return, newest first.",
    )


class InvestigationReportArgs(ToolArgs):
    """Arguments of ``investigation_get_report``."""

    namespace: Namespace
    investigation_id: InvestigationId


def _source(backends: ToolBackends) -> InvestigationSource:
    if backends.investigations is None:
        raise ToolError("failed", "investigations", "no investigation source configured")
    return backends.investigations


def _row(row: InvestigationRow) -> dict[str, Any]:
    return {
        "investigation_id": row.id,
        "status": row.status,
        "case": row.case_id,
        "alert": row.alert_name,
        "namespace": row.namespace,
        "pod": row.pod,
        "started_at": format_time(row.started_at),
        "finished_at": format_time(row.finished_at),
        "report_status": row.report_status,
    }


@tool(
    name="investigation_list",
    kind="auxiliary",
    read_only=True,
    timeout_s=10,
    args=InvestigationListArgs,
)
async def investigation_list(args: InvestigationListArgs, backends: ToolBackends) -> ToolOutput:
    """List the latest investigations whose alert is in a namespace, newest first.

    Each entry carries the investigation ID, its status, the replayed case (none for a
    real alert), the alert name, the Pod, the start and end times and the report status
    (published, degraded or none).
    """
    rows = await _source(backends).list_investigations(args.namespace, args.limit)
    return ToolOutput(
        render({"namespace": args.namespace, "investigations": [_row(row) for row in rows]}),
        identifiers=collect_identifiers(
            (row.id for row in rows), (row.alert_name for row in rows), (row.pod for row in rows)
        ),
    )


@tool(
    name="investigation_get_report",
    kind="auxiliary",
    read_only=True,
    timeout_s=10,
    args=InvestigationReportArgs,
)
async def investigation_get_report(
    args: InvestigationReportArgs, backends: ToolBackends
) -> ToolOutput:
    """Read the report of one investigation: published, or degraded when none passed the gate.

    The report is the one the detail page shows: summary, impact, timeline, root cause,
    claims, missing evidence, suggestions, references and follow-up questions, every
    statement citing evidence IDs.
    """
    found = await _source(backends).get_report(args.investigation_id)
    if found is None or found.investigation.namespace != args.namespace:
        raise ToolError("not_found", f"investigation {args.investigation_id} in {args.namespace}")
    if found.report is None:
        raise ToolError("not_found", f"report of investigation {args.investigation_id}")
    return ToolOutput(
        render({**_row(found.investigation), "report": found.report}),
        identifiers=collect_identifiers(
            (found.investigation.id, found.investigation.pod, found.investigation.alert_name),
            found.identifiers,
        ),
    )
