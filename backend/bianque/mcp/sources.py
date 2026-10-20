"""The stored investigations as the auxiliary tools read them, from PostgreSQL.

An investigation belongs to the namespace named by its alert's ``namespace`` label; an
investigation without one belongs to no namespace and is never listed or read.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bianque.storage.models import Alert, Evidence, Investigation, Report
from bianque.storage.repositories import ReportRepository
from bianque.tools.backends.base import InvestigationReport, InvestigationRow

#: Report states that end an investigation's reporting.
_FINAL = ("published", "degraded")


def _row(
    investigation: Investigation, alert: Alert | None, report_status: str | None
) -> InvestigationRow:
    labels = alert.labels if alert is not None else {}
    return InvestigationRow(
        id=investigation.id,
        status=investigation.status,
        case_id=investigation.case_id,
        alert_name=labels.get("alertname"),
        namespace=labels.get("namespace"),
        pod=labels.get("pod"),
        started_at=investigation.started_at,
        finished_at=investigation.finished_at,
        report_status=report_status,
    )


class DatabaseInvestigations:
    """Reads investigations, their alerts, final reports and evidence identifiers."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """Read through ``sessions``, one short session per call."""
        self._sessions = sessions

    async def list_investigations(self, namespace: str, limit: int) -> list[InvestigationRow]:
        """Return the latest investigations whose alert is in ``namespace``, newest first."""
        async with self._sessions() as session:
            pairs = (
                await session.execute(
                    select(Investigation, Alert)
                    .join(Alert, Alert.id == Investigation.alert_id)
                    .where(Alert.labels["namespace"].astext == namespace)
                    .order_by(Investigation.started_at.desc(), Investigation.id.desc())
                    .limit(limit)
                )
            ).all()
            rows = await session.execute(
                select(Report.investigation_id, Report.status).where(
                    Report.investigation_id.in_([inv.id for inv, _ in pairs]),
                    Report.status.in_(_FINAL),
                )
            )
            finals = {investigation_id: status for investigation_id, status in rows.all()}
        return [_row(inv, alert, finals.get(inv.id)) for inv, alert in pairs]

    async def get_report(self, investigation_id: str) -> InvestigationReport | None:
        """Return an investigation with its final report and its evidence identifiers."""
        async with self._sessions() as session:
            found = (
                await session.execute(
                    select(Investigation, Alert)
                    .outerjoin(Alert, Alert.id == Investigation.alert_id)
                    .where(Investigation.id == investigation_id)
                )
            ).first()
            if found is None:
                return None
            investigation, alert = found
            final = await ReportRepository(session).get_final(investigation.id)
            identifiers = (
                await session.scalars(
                    select(Evidence.identifiers).where(
                        Evidence.investigation_id == investigation.id
                    )
                )
            ).all()
        labels = alert.labels if alert is not None else {}
        return InvestigationReport(
            investigation=_row(investigation, alert, final.status if final is not None else None),
            report=final.body if final is not None else None,
            identifiers=tuple(
                sorted(
                    {str(item) for group in identifiers for item in group}
                    | {str(value) for value in labels.values()}
                )
            ),
        )
