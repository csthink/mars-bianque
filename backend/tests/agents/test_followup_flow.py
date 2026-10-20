"""The follow-up flow on a scripted S1-a investigation."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from investigation_harness import (
    CASES,
    Investigated,
    database,
    investigate,
    no_wait,
    scripted_settings,
)
from sqlalchemy import Engine, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bianque.agents.followup import (
    Content,
    Done,
    Failed,
    FollowupEvent,
    FollowupService,
    NoReportError,
    Progress,
    QuestionNotRecordedError,
    SessionBusyError,
)
from bianque.evals.cases import load_question_set
from bianque.knowledge.search import SearchResult
from bianque.llm import ScriptedReply, ScriptedRule
from bianque.llm.errors import RetryableProviderError
from bianque.llm.request import ModelRequest
from bianque.storage.models import CallRecord, FollowupMessage, Investigation
from bianque.storage.repositories import FollowupRepository, InvestigationRepository

#: A text no role rule writes; it marks model output that must not reach anyone.
SENTINEL = "SENTINEL-f0110w"


def answer(*segments: dict[str, Any], references: Sequence[str] = ()) -> ScriptedReply:
    """The reply that submits an answer of ``segments``."""
    return ScriptedReply(
        tool_calls=(
            ("submit_answer", {"segments": list(segments), "references": list(references)}),
        )
    )


def segment(text: str, *citations: str, insufficient: bool = False) -> dict[str, Any]:
    return {"text": text, "citations": list(citations), "insufficient_evidence": insufficient}


def followup_only(reply: ScriptedRule) -> ScriptedRule:
    """Apply ``reply`` to follow-up calls only."""

    def rule(request: ModelRequest) -> ScriptedReply | None:
        return reply(request) if request.agent == "followup" else None

    return rule


async def collect(events: AsyncIterator[FollowupEvent]) -> list[FollowupEvent]:
    return [event async for event in events]


@pytest.fixture
async def s1a(migrated: Engine, pg_url: str) -> Investigated:
    return await investigate(pg_url, "s1-a")


@pytest.fixture
async def sessions(pg_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database(pg_url) as factory:
        yield factory


def service(
    pg_url: str,
    sessions: async_sessionmaker[AsyncSession],
    rules: Sequence[ScriptedRule] = (),
    **overrides: Any,
) -> FollowupService:
    return FollowupService(
        scripted_settings(pg_url, **overrides), sessions, rules=rules, model_sleep=no_wait
    )


async def open_session(followups: FollowupService, s1a: Investigated) -> int:
    created = await followups.create_session(s1a.investigation.id, created_by="demo")
    return created.id


async def messages(sessions: async_sessionmaker[AsyncSession], session_id: int) -> list[Any]:
    async with sessions() as session:
        return list(await FollowupRepository(session).list_messages(session_id))


async def test_an_answer_is_validated_stored_then_released(
    s1a: Investigated, pg_url: str, sessions: async_sessionmaker[AsyncSession]
) -> None:
    followups = service(pg_url, sessions)
    session_id = await open_session(followups, s1a)

    events = await collect(await followups.ask(session_id, "Why this commit?"))

    stages = [e.stage for e in events if isinstance(e, Progress)]
    assert stages[:3] == ["retrieving", "generating", "validating"]
    contents = [e for e in events if isinstance(e, Content)]
    assert contents and isinstance(events[-1], Done)
    last_validating = max(
        i for i, e in enumerate(events) if getattr(e, "stage", "") == "validating"
    )
    assert events.index(contents[0]) > last_validating
    stored = await messages(sessions, session_id)
    assert [m.role for m in stored] == ["user", "assistant"]
    question, reply = stored
    assert question.content == "Why this commit?" and question.segments == []
    assert reply.id == events[-1].message_id
    assert [s["text"] for s in reply.segments] == [c.text for c in contents]
    assert reply.content == "\n\n".join(c.text for c in contents)
    evidence_ids = {e.id for e in s1a.evidence}
    cited = {c for s in reply.segments for c in s["citations"]}
    assert cited and all(c in evidence_ids or c.startswith("sg_") for c in cited)
    assert reply.citations == list(dict.fromkeys(c for s in reply.segments for c in s["citations"]))


async def test_follow_up_calls_belong_to_the_session_not_the_investigation(
    s1a: Investigated, pg_url: str, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The retrieval is a knowledge call of the session; tokens stay with it."""
    followups = service(pg_url, sessions)
    session_id = await open_session(followups, s1a)
    await collect(await followups.ask(session_id, "Why this commit?"))

    async with sessions() as session:
        calls = list(
            await session.scalars(select(CallRecord).where(CallRecord.session_id == session_id))
        )
        investigation = await session.get_one(Investigation, s1a.investigation.id)
    assert {(c.agent, c.kind, c.name) for c in calls} == {
        ("followup", "knowledge", "retrieve_knowledge"),
        ("followup", "model", "answer"),
    }
    assert all(c.investigation_id is None for c in calls)
    assert investigation.tokens_used == s1a.investigation.tokens_used
    model_call = next(c for c in calls if c.kind == "model")
    assert model_call.gate_result is not None and model_call.gate_result["passed"] is True


async def test_the_second_turn_sends_the_first_question_and_answer(
    s1a: Investigated, pg_url: str, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """In scripted mode the model sees the earlier turn, so «it» can refer to it."""
    requests: list[ModelRequest] = []

    def capture(request: ModelRequest) -> ScriptedReply | None:
        if request.agent == "followup":
            requests.append(request)
        return None

    followups = service(pg_url, sessions, rules=[capture])
    session_id = await open_session(followups, s1a)
    await collect(await followups.ask(session_id, "Which commit broke it?"))
    await collect(await followups.ask(session_id, "What did it change?"))

    first, second = requests
    assert [m.role for m in first.prompt.messages] == ["system", "user", "user"]
    assert [m.role for m in second.prompt.messages] == [
        "system",
        "user",
        "user",
        "assistant",
        "user",
    ]
    earlier = (await messages(sessions, session_id))[1]
    assert second.prompt.messages[2].content.text == "Which commit broke it?"
    replayed = second.prompt.messages[3].content.text
    for item in earlier.segments:
        assert item["text"] in replayed
        assert ", ".join(item["citations"]) in replayed
    assert second.prompt.messages[4].content.text == "What did it change?"

    # Evidence, report and suggestions come from the database and are the same in both
    # turns; the knowledge is retrieved for each question.
    def data(request: ModelRequest) -> str:
        return request.prompt.messages[1].content.text.split("## Knowledge")[0]

    assert data(first) == data(second)


async def test_the_prompt_holds_evidence_report_suggestions_and_knowledge(
    s1a: Investigated, pg_url: str, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Knowledge retrieved for the question is injected into the answer's prompt."""
    requests: list[ModelRequest] = []

    def capture(request: ModelRequest) -> ScriptedReply | None:
        if request.agent == "followup":
            requests.append(request)
        return None

    followups = service(pg_url, sessions, rules=[capture])
    session_id = await open_session(followups, s1a)
    await collect(
        await followups.ask(session_id, "How do I read the events of a crash looping pod?")
    )
    data = requests[0].prompt.messages[1].content.text
    for heading in ("## Evidence", "## Report", "## Suggestions", "## Knowledge"):
        assert heading in data
    assert "[kn_" in data
    assert s1a.investigation.id not in data
    assert all(f"[{e.id}]" in data for e in s1a.evidence)


@pytest.mark.parametrize(
    ("bad", "validator"),
    [
        (segment(f"{SENTINEL} it was the commit", "ev_ffffffffff"), "citation_integrity"),
        (segment(f"{SENTINEL} run kubectl delete pod s1-app", "EV"), "forbidden_actions"),
        (segment(f"{SENTINEL} the runbook says so", "kn_000000000000"), "knowledge_boundary"),
    ],
)
async def test_a_rejected_answer_fails_and_is_not_stored(
    s1a: Investigated,
    pg_url: str,
    sessions: async_sessionmaker[AsyncSession],
    bad: dict[str, Any],
    validator: str,
) -> None:
    """Fake evidence, a write command or knowledge as a citation is rejected."""
    evidence_id = s1a.evidence[0].id
    bad = {**bad, "citations": [evidence_id if c == "EV" else c for c in bad["citations"]]}
    followups = service(pg_url, sessions, rules=[followup_only(lambda r: answer(bad))])
    session_id = await open_session(followups, s1a)

    events = await collect(await followups.ask(session_id, "Why?"))

    last = events[-1]
    assert isinstance(last, Failed) and last.reason == "gate_rejected"
    assert validator in last.violations
    assert not any(isinstance(e, Content) for e in events)
    assert [e.stage for e in events if isinstance(e, Progress)].count("retrying") == 1
    assert await messages(sessions, session_id) == []
    assert all(SENTINEL not in repr(e) for e in events)


async def test_an_answer_accepted_on_the_retry_is_stored_once(
    s1a: Investigated, pg_url: str, sessions: async_sessionmaker[AsyncSession]
) -> None:
    attempts: list[int] = []
    good = segment("The commit changed the environment.", s1a.evidence[0].id)

    def first_bad(request: ModelRequest) -> ScriptedReply | None:
        if request.agent != "followup":
            return None
        attempts.append(1)
        return answer(segment(SENTINEL, "ev_ffffffffff")) if len(attempts) == 1 else answer(good)

    followups = service(pg_url, sessions, rules=[first_bad])
    session_id = await open_session(followups, s1a)
    events = await collect(await followups.ask(session_id, "Why?"))

    assert isinstance(events[-1], Done) and len(attempts) == 2
    stored = await messages(sessions, session_id)
    assert [m.role for m in stored] == ["user", "assistant"]
    assert stored[1].segments == [good]
    assert all(SENTINEL not in repr(e) for e in events)


async def test_an_unavailable_model_fails_without_text(
    s1a: Investigated, pg_url: str, sessions: async_sessionmaker[AsyncSession]
) -> None:
    def unavailable(request: ModelRequest) -> ScriptedReply | None:
        raise RetryableProviderError("timeout", "the model did not answer")

    followups = service(pg_url, sessions, rules=[followup_only(unavailable)])
    session_id = await open_session(followups, s1a)
    events = await collect(await followups.ask(session_id, "Why?"))
    assert events[-1] == Failed("model_unavailable")
    assert await messages(sessions, session_id) == []


async def test_a_failing_retrieval_still_answers(
    s1a: Investigated, pg_url: str, sessions: async_sessionmaker[AsyncSession]
) -> None:
    class Broken:
        async def search(self, query: str, limit: int = 5) -> SearchResult:
            raise RuntimeError("vector store down")

    followups = FollowupService(
        scripted_settings(pg_url), sessions, model_sleep=no_wait, knowledge=Broken()
    )
    session_id = await open_session(followups, s1a)
    events = await collect(await followups.ask(session_id, "Why?"))
    assert isinstance(events[-1], Done)
    async with sessions() as session:
        retrieval = await session.scalar(
            select(CallRecord).where(
                CallRecord.session_id == session_id, CallRecord.kind == "knowledge"
            )
        )
    assert retrieval is not None and retrieval.status == "error"
    assert retrieval.error == "RuntimeError"


async def test_a_used_up_budget_answers_without_calling_the_model(
    s1a: Investigated, pg_url: str, sessions: async_sessionmaker[AsyncSession]
) -> None:
    followups = service(pg_url, sessions, followup_token_budget=1)
    session_id = await open_session(followups, s1a)
    first = await collect(await followups.ask(session_id, "Why?"))
    assert isinstance(first[-1], Done)

    second = await collect(await followups.ask(session_id, "And then?"))

    assert second == [Failed("budget_exhausted")]
    async with sessions() as session:
        model_calls = await session.scalar(
            select(func.count())
            .select_from(CallRecord)
            .where(CallRecord.session_id == session_id, CallRecord.kind == "model")
        )
    assert model_calls == 1


async def test_a_session_answers_one_question_at_a_time(
    s1a: Investigated, pg_url: str, sessions: async_sessionmaker[AsyncSession]
) -> None:
    followups = service(pg_url, sessions)
    session_id = await open_session(followups, s1a)
    events = await followups.ask(session_id, "Why?")
    with pytest.raises(SessionBusyError):
        await followups.ask(session_id, "And?")
    await collect(events)
    await collect(await followups.ask(session_id, "And?"))


async def test_a_session_needs_a_report(
    migrated: Engine, pg_url: str, sessions: async_sessionmaker[AsyncSession]
) -> None:
    async with sessions() as session, session.begin():
        pending = await InvestigationRepository(session).create(
            trigger="manual", tool_backend="pack", llm_mode="scripted", token_budget=1
        )
    with pytest.raises(NoReportError):
        await service(pg_url, sessions).create_session(pending.id, created_by="demo")


def _questions(tmp_path: Path, *questions: str) -> Path:
    cases = tmp_path / "cases"
    (cases / "s1-a").mkdir(parents=True)
    items = [{"id": f"q{i}", "question": q} for i, q in enumerate(questions, start=1)]
    (cases / "s1-a" / "questions.yaml").write_text(
        yaml.safe_dump({"schema_version": 2, "questions": items}, allow_unicode=True)
    )
    return cases


async def test_recorded_mode_takes_the_next_recorded_question_only(
    s1a: Investigated, pg_url: str, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """The n-th turn asks the n-th question; a missing recording is a model error."""
    cases = _questions(tmp_path, "First?", "Second?")
    followups = service(
        pg_url,
        sessions,
        llm_mode="recorded",
        cases_dir=cases,
        llm_recordings_dir=tmp_path / "recordings",
    )
    session_id = await open_session(followups, s1a)
    with pytest.raises(QuestionNotRecordedError) as wrong:
        await followups.ask(session_id, "Second?")
    assert wrong.value.expected == "First?"

    events = await collect(await followups.ask(session_id, "First?"))

    assert events[-1] == Failed("model_error")
    assert await messages(sessions, session_id) == []


def test_the_s1a_question_set_is_well_formed() -> None:
    questions = load_question_set(CASES / "s1-a").questions
    assert [q.id for q in questions] == ["q1", "q2", "q3", "q4"]
    by_id = {q.id: q for q in questions}
    assert by_id["q2"].depends_on == "q1"
    assert all(q.expected_evidence or q.expected_suggestions or q.expect_unknown for q in questions)


async def test_the_s1a_annotations_name_evidence_of_the_scripted_investigation(
    s1a: Investigated,
) -> None:
    """Each annotated evidence of the S1-a questions is evidence the investigation collects."""
    annotated = [
        spec for q in load_question_set(CASES / "s1-a").questions for spec in q.expected_evidence
    ]
    assert annotated
    for spec in annotated:
        assert any(spec.matches(e.tool, e.meta.get("arguments", {})) for e in s1a.evidence), spec


async def test_a_message_count_matches_the_turns(
    s1a: Investigated, pg_url: str, sessions: async_sessionmaker[AsyncSession]
) -> None:
    followups = service(pg_url, sessions)
    session_id = await open_session(followups, s1a)
    for question in ("One?", "Two?", "Three?"):
        await collect(await followups.ask(session_id, question))
    async with sessions() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(FollowupMessage)
            .where(FollowupMessage.session_id == session_id)
        )
    assert count == 6


async def test_an_answer_may_refer_to_the_knowledge_the_report_refers_to(
    s1a: Investigated, pg_url: str, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The prompt shows the report with its references; the answer may list them too."""
    assert s1a.final is not None
    referenced = [item["knowledge_id"] for item in s1a.final.body["references"]]
    assert referenced
    reply = answer(segment("The commit changed it.", s1a.evidence[0].id), references=referenced)

    class Nothing:
        async def search(self, query: str, limit: int = 5) -> SearchResult:
            return SearchResult(hits=[], degraded=None)

    followups = FollowupService(
        scripted_settings(pg_url),
        sessions,
        rules=[followup_only(lambda r: reply)],
        model_sleep=no_wait,
        knowledge=Nothing(),
    )
    session_id = await open_session(followups, s1a)
    events = await collect(await followups.ask(session_id, "Why?"))
    assert isinstance(events[-1], Done)
    assert (await messages(sessions, session_id))[1].references == referenced
