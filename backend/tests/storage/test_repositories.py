from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from bianque.storage import ids
from bianque.storage.errors import AlreadyDecidedError, NotFoundError
from bianque.storage.models import EMBEDDING_DIMENSION, Confirmation, KnowledgeChunk
from bianque.storage.repositories import (
    SUMMARY_LIMIT,
    AlertRepository,
    AssessmentRepository,
    CallRecordRepository,
    ChunkSpec,
    ConfirmationRepository,
    EventRepository,
    EvidenceRepository,
    FollowupRepository,
    InvestigationRepository,
    KnowledgeRepository,
    PlanRepository,
    ReportRepository,
    StepSpec,
    SuggestionRepository,
    SuggestionSpec,
)

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


async def _alert(session: AsyncSession, fingerprint: str = "fp-1") -> int:
    recorded = await AlertRepository(session).record(
        fingerprint=fingerprint,
        status="firing",
        labels={"alertname": "PodCrashLooping"},
        annotations={},
        starts_at=NOW,
        payload={"fingerprint": fingerprint},
    )
    return recorded.alert.id


async def _investigation(session: AsyncSession, alert_id: int | None = None) -> str:
    investigation = await InvestigationRepository(session).create(
        trigger="alert" if alert_id else "manual",
        alert_id=alert_id,
        tool_backend="pack",
        llm_mode="scripted",
        token_budget=1000,
    )
    return investigation.id


async def _suggestions(session: AsyncSession) -> tuple[str, list[str]]:
    investigation_id = await _investigation(session)
    report = await ReportRepository(session).add_attempt(
        investigation_id, status="published", body={"root_cause": "x"}, schema_version="1"
    )
    stored = await SuggestionRepository(session).add_all(
        investigation_id,
        report.id,
        [
            SuggestionSpec(action="Inspect events", risk="read_only", citations=["ev_a"]),
            SuggestionSpec(action="Roll back", risk="change", command="kubectl rollout undo"),
            SuggestionSpec(action="Raise the limit", risk="change", citations=["ev_b"]),
        ],
    )
    await session.commit()
    return investigation_id, [s.id for s in stored]


async def test_repeated_firing_during_open_investigation_only_refreshes(
    session: AsyncSession,
) -> None:
    alerts = AlertRepository(session)
    alert_id = await _alert(session)
    investigation_id = await _investigation(session, alert_id)
    await session.commit()
    first = await alerts.get(alert_id)
    first_received = first.received_at

    again = await alerts.record(
        fingerprint="fp-1", status="firing", labels={}, annotations={}, starts_at=NOW, payload={}
    )
    await session.commit()
    assert again.under_investigation
    assert again.alert.id == alert_id
    assert again.alert.received_at > first_received

    await InvestigationRepository(session).set_status(investigation_id, "reported")
    after = await alerts.record(
        fingerprint="fp-1", status="firing", labels={}, annotations={}, starts_at=NOW, payload={}
    )
    assert not after.under_investigation
    assert after.alert.id != alert_id


async def test_confirmations_reject_update_and_delete(session: AsyncSession) -> None:
    investigation_id, suggestion_ids = await _suggestions(session)
    confirmation = await ConfirmationRepository(session).decide(
        investigation_id, suggestion_ids[0], decision="confirmed", actor="demo"
    )
    await session.commit()
    confirmation_id = confirmation.id

    for statement in (
        "UPDATE confirmations SET decision = 'rejected' WHERE id = :id",
        "DELETE FROM confirmations WHERE id = :id",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(text(statement), {"id": confirmation_id})
        await session.rollback()

    stored = await session.scalar(select(Confirmation).where(Confirmation.id == confirmation_id))
    assert stored is not None and stored.decision == "confirmed"


async def test_each_suggestion_is_decided_once_and_state_is_derived(
    session: AsyncSession,
) -> None:
    investigation_id, suggestion_ids = await _suggestions(session)
    confirmations = ConfirmationRepository(session)
    await confirmations.decide(investigation_id, suggestion_ids[0], decision="confirmed", actor="a")
    await confirmations.decide(investigation_id, suggestion_ids[1], decision="rejected", actor="a")
    await session.commit()

    with pytest.raises(AlreadyDecidedError):
        await confirmations.decide(
            investigation_id, suggestion_ids[0], decision="rejected", actor="b"
        )
    await session.commit()

    states = await SuggestionRepository(session).list_with_decisions(investigation_id)
    assert [s.suggestion.id for s in states] == suggestion_ids
    assert [s.state for s in states] == ["confirmed", "rejected", "pending"]


async def test_suggestion_ids_are_derived_from_content(session: AsyncSession) -> None:
    _, suggestion_ids = await _suggestions(session)
    assert suggestion_ids[1] == ids.suggestion_id(
        2, "Roll back", "kubectl rollout undo", "change", []
    )


async def test_identical_evidence_is_stored_once(session: AsyncSession) -> None:
    investigation_id = await _investigation(session)
    repository = EvidenceRepository(session)
    evidence_id = ids.evidence_id("get_pod", {"name": "s1-app"}, "redacted")
    first = await repository.add(
        investigation_id,
        evidence_id=evidence_id,
        source="kubernetes",
        tool="get_pod",
        collected_at=NOW,
        raw_content="raw",
        redacted_content="redacted",
        identifiers=["s1-app"],
    )
    second = await repository.add(
        investigation_id,
        evidence_id=evidence_id,
        source="kubernetes",
        tool="get_pod",
        collected_at=datetime(2026, 10, 19, 12, 5, tzinfo=UTC),
        raw_content="raw",
        redacted_content="redacted",
    )
    await session.commit()
    assert second.collected_at == first.collected_at == NOW
    assert [e.id for e in await repository.list(investigation_id)] == [evidence_id]
    with pytest.raises(NotFoundError):
        await repository.get(investigation_id, "ev_missing")


async def test_evidence_is_listed_by_id(session: AsyncSession) -> None:
    investigation_id = await _investigation(session)
    repository = EvidenceRepository(session)
    for content in ("c", "a", "b"):
        await repository.add(
            investigation_id,
            evidence_id=ids.evidence_id("get_logs", {}, content),
            source="kubernetes",
            collected_at=NOW,
            raw_content=content,
            redacted_content=content,
        )
    listed = [e.id for e in await repository.list(investigation_id)]
    assert listed == sorted(listed)


async def test_plan_versions_and_steps(session: AsyncSession) -> None:
    investigation_id = await _investigation(session)
    plans = PlanRepository(session)
    first = await plans.add_plan(
        investigation_id,
        source="model",
        rationale="initial",
        steps=[
            StepSpec(domain="workload", goal="Read pod status", agent="workload"),
            StepSpec(domain="change", goal="Diff recent commits", agent="change"),
        ],
    )
    second = await plans.add_plan(
        investigation_id,
        source="default",
        rationale="replan",
        steps=[StepSpec(domain="logs", goal="Read logs", agent="logs")],
    )
    assert (first.version, second.version) == (1, 2)
    steps = await plans.list_steps(investigation_id)
    assert [(s.plan_id, s.seq, s.status) for s in steps] == [
        (first.id, 1, "pending"),
        (first.id, 2, "pending"),
        (second.id, 1, "pending"),
    ]
    await plans.update_step(steps[0].id, status="done", findings=[{"claim": "x"}], finished_at=NOW)
    await session.commit()
    await session.refresh(steps[0])
    assert steps[0].status == "done" and steps[0].findings == [{"claim": "x"}]


async def test_assessments_and_tokens(session: AsyncSession) -> None:
    investigation_id = await _investigation(session)
    assessments = AssessmentRepository(session)
    await assessments.add(investigation_id, round=1, sufficient=False, reason="r", missing=["x"])
    await assessments.add(
        investigation_id, round=2, sufficient=True, reason="r2", rule_override=True
    )
    investigations = InvestigationRepository(session)
    await investigations.add_tokens(investigation_id, 120)
    await investigations.add_tokens(investigation_id, 30)
    await session.commit()
    rounds = await assessments.list(investigation_id)
    assert [(a.round, a.sufficient, a.rule_override) for a in rounds] == [
        (1, False, False),
        (2, True, True),
    ]
    investigation = await investigations.get(investigation_id)
    await session.refresh(investigation)
    assert investigation.tokens_used == 150
    assert investigation.status == "pending"


async def test_events_are_numbered_without_gaps(session: AsyncSession) -> None:
    investigation_id = await _investigation(session)
    other_id = await _investigation(session)
    events = EventRepository(session)
    for event_type in ("investigation_started", "plan_created", "step_started"):
        await events.append(investigation_id, event_type, {})
    await events.append(other_id, "investigation_started", {})
    await session.commit()
    assert [e.seq for e in await events.list_after(investigation_id)] == [1, 2, 3]
    assert [e.type for e in await events.list_after(investigation_id, 2)] == ["step_started"]
    assert [e.seq for e in await events.list_after(other_id)] == [1]


async def test_report_attempts(session: AsyncSession) -> None:
    investigation_id = await _investigation(session)
    reports = ReportRepository(session)
    assert await reports.get_final(investigation_id) is None
    await reports.add_attempt(
        investigation_id,
        status="rejected",
        body={},
        schema_version="1",
        gate_result={"violations": ["citation"]},
    )
    published = await reports.add_attempt(
        investigation_id, status="published", body={"ok": True}, schema_version="1"
    )
    await session.commit()
    assert [r.attempt for r in await reports.list_attempts(investigation_id)] == [1, 2]
    final = await reports.get_final(investigation_id)
    assert final is not None and final.id == published.id


async def test_call_summaries_are_truncated(session: AsyncSession) -> None:
    investigation_id = await _investigation(session)
    calls = CallRecordRepository(session)
    await calls.add(
        investigation_id=investigation_id,
        agent="workload",
        kind="tool",
        name="get_pod",
        input_summary="i" * (SUMMARY_LIMIT + 10),
        output_summary="o" * (SUMMARY_LIMIT + 10),
        status="ok",
        started_at=NOW,
    )
    await session.commit()
    (record,) = await calls.list_for_investigation(investigation_id)
    assert len(record.input_summary) == SUMMARY_LIMIT
    assert record.output_summary is not None and len(record.output_summary) == SUMMARY_LIMIT


async def test_followup_messages_are_numbered(session: AsyncSession) -> None:
    investigation_id = await _investigation(session)
    followups = FollowupRepository(session)
    followup = await followups.create_session(investigation_id, created_by="demo")
    await followups.add_message(followup.id, role="user", content="Why?")
    await followups.add_message(
        followup.id, role="assistant", content="Because", citations=["ev_a"], unknown=False
    )
    await session.commit()
    messages = await followups.list_messages(followup.id)
    assert [(m.seq, m.role) for m in messages] == [(1, "user"), (2, "assistant")]
    assert (await followups.get_session(followup.id)).investigation_id == investigation_id


async def test_knowledge_import_is_idempotent_and_searchable(session: AsyncSession) -> None:
    knowledge = KnowledgeRepository(session)
    vector = [0.0] * EMBEDDING_DIMENSION
    chunks = [
        ChunkSpec("OOMKilled > Diagnosis", "Read the pod events.", vector),
        ChunkSpec("Probes > Tuning", "Container OOMKilled after a change.", vector),
    ]
    document = await knowledge.upsert_document(path="runbooks/oom.md", title="OOM", sha256="a")
    first = await knowledge.replace_chunks(document, chunks)
    again = await knowledge.upsert_document(path="runbooks/oom.md", title="OOM v2", sha256="b")
    second = await knowledge.replace_chunks(again, chunks)
    await session.commit()

    assert again.id == document.id and again.title == "OOM v2"
    assert [c.id for c in first] == [c.id for c in second]
    assert first[0].id == ids.knowledge_id("runbooks/oom.md", "OOMKilled > Diagnosis", 0)

    query = text(
        "SELECT id FROM knowledge_chunks WHERE tsv @@ plainto_tsquery('english', :q) "
        "ORDER BY ts_rank(tsv, plainto_tsquery('english', :q)) DESC"
    )
    ranked = list(await session.scalars(query, {"q": "oomkilled"}))
    assert ranked == [first[0].id, first[1].id]
    stored = await session.scalars(
        select(KnowledgeChunk.id).where(KnowledgeChunk.document_id == document.id)
    )
    assert len(stored.all()) == len(chunks)
