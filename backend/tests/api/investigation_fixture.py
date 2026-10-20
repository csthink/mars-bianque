"""Load the handwritten S1-a investigation fixture into a database.

The fixture (``tests/fixtures/investigation/s1a.json``) is one complete investigation:
evidence collected by the real tools from the S1-a evidence pack, and a handwritten
plan, assessments, call records and report. Rows are written with fixed IDs and times,
so pages and screenshots are the same on every run. Loading again replaces the earlier
copy. The API tests import ``load``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bianque.storage import ids
from bianque.storage.models import (
    Alert,
    Assessment,
    CallRecord,
    Evidence,
    Investigation,
    Plan,
    Report,
    Step,
    Suggestion,
)
from bianque.storage.repositories import truncate_summary

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "investigation" / "s1a.json"


def read_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    """Return the fixture as JSON."""
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def _time(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


async def load(session: AsyncSession, path: Path = FIXTURE) -> str:
    """Write the fixture investigation and return its ID; commits the session."""
    data = read_fixture(path)
    inv = data["investigation"]
    alert_data = data["alert"]
    # Replace only the earlier copy of the fixture: real investigations of the same alert
    # may share the database (and the fingerprint) and must stay.
    earlier_alert = await session.scalar(
        select(Investigation.alert_id).where(Investigation.id == inv["id"])
    )
    await session.execute(delete(Suggestion).where(Suggestion.investigation_id == inv["id"]))
    await session.execute(delete(Investigation).where(Investigation.id == inv["id"]))
    if earlier_alert is not None:
        await session.execute(delete(Alert).where(Alert.id == earlier_alert))

    alert = Alert(
        fingerprint=alert_data["fingerprint"],
        status=alert_data["status"],
        labels=alert_data["labels"],
        annotations=alert_data["annotations"],
        starts_at=_time(alert_data["starts_at"]),
        received_at=_time(alert_data["received_at"]),
        payload=alert_data["payload"],
    )
    session.add(alert)
    await session.flush()
    session.add(
        Investigation(
            **{key: value for key, value in inv.items() if not key.endswith("_at")},
            alert_id=alert.id,
            started_at=_time(inv["started_at"]),
            finished_at=_time(inv["finished_at"]),
        )
    )

    steps: dict[tuple[int, int], Step] = {}
    for plan_data in data["plans"]:
        plan = Plan(
            investigation_id=inv["id"],
            version=plan_data["version"],
            source=plan_data["source"],
            rationale=plan_data["rationale"],
            created_at=_time(plan_data["created_at"]),
        )
        session.add(plan)
        await session.flush()
        for step_data in plan_data["steps"]:
            step = Step(
                plan_id=plan.id,
                investigation_id=inv["id"],
                **{k: v for k, v in step_data.items() if not k.endswith("_at")},
                started_at=_time(step_data["started_at"]),
                finished_at=_time(step_data["finished_at"]),
            )
            session.add(step)
            steps[(plan_data["version"], step_data["seq"])] = step
    for item in data["assessments"]:
        session.add(
            Assessment(
                investigation_id=inv["id"],
                **{k: v for k, v in item.items() if k != "created_at"},
                created_at=_time(item["created_at"]),
            )
        )

    calls: dict[str, CallRecord] = {}
    for call in data["calls"]:
        record = CallRecord(
            investigation_id=inv["id"],
            **{k: v for k, v in call.items() if k not in ("key", "started_at")},
            started_at=_time(call["started_at"]),
        )
        record.input_summary = truncate_summary(record.input_summary) or ""
        record.output_summary = truncate_summary(record.output_summary)
        session.add(record)
        calls[call["key"]] = record
    await session.flush()

    for item in data["evidence"]:
        step_key = item["step"]
        session.add(
            Evidence(
                investigation_id=inv["id"],
                id=item["id"],
                step_id=None if step_key is None else steps[(step_key[0], step_key[1])].id,
                call_id=None if item["call"] is None else calls[item["call"]].id,
                source=item["source"],
                tool=item["tool"],
                collected_at=_time(item["collected_at"]),
                raw_content=item["raw_content"],
                redacted_content=item["redacted_content"],
                identifiers=item["identifiers"],
                meta=item["meta"],
            )
        )

    for item in data["reports"]:
        report = Report(
            investigation_id=inv["id"],
            attempt=item["attempt"],
            status=item["status"],
            body=item["body"],
            gate_result=item["gate_result"],
            schema_version=item["body"]["schema_version"],
            created_at=_time(item["created_at"]),
        )
        session.add(report)
        await session.flush()
        if item["status"] == "published":
            for seq, suggestion in enumerate(item["body"]["suggestions"], start=1):
                session.add(
                    Suggestion(
                        investigation_id=inv["id"],
                        id=ids.suggestion_id(
                            seq,
                            suggestion["action"],
                            suggestion.get("command"),
                            suggestion["risk"],
                            suggestion["citations"],
                        ),
                        report_id=report.id,
                        seq=seq,
                        action=suggestion["action"],
                        command=suggestion.get("command"),
                        risk=suggestion["risk"],
                        citations=suggestion["citations"],
                    )
                )
    await session.commit()
    loaded = await session.scalar(select(Investigation.id).where(Investigation.id == inv["id"]))
    assert loaded == inv["id"]
    return str(loaded)
