"""Follow-up questions about a finished investigation.

A question is answered by one short flow, not by a long-running graph: read the
investigation's evidence, report and suggestions and the session's latest turns from
the database, retrieve runbook knowledge for the question, generate the answer by a
forced function call, pass it through the evidence gate (retrying once after a
rejection), store it, and only then release its text. The memory of a session is its
rows in the database, so a restarted process answers with the same context.

The flow reports what it is doing as events. Until the answer has passed the gate, the
events carry stages, durations, reasons and validator names, never model text.
The flow runs in its own task: a client that goes away does not cancel it, so an
answer that was paid for is still validated and stored.

Prompts are built the same way on every run so that recorded responses match: no time,
no investigation or session ID, evidence sorted by ID, and the earlier turns in their
order.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bianque.agents.gated import MODEL_UNAVAILABLE, Stage, generate_gated
from bianque.agents.nodes import render_knowledge_texts, summarize_retrieval
from bianque.agents.prompting import (
    EVIDENCE,
    KNOWLEDGE,
    REPORT,
    SUGGESTIONS,
    EvidenceView,
    SuggestionView,
    prompt_text,
    render_answer,
    render_evidence,
    render_json,
    render_suggestions,
    section,
)
from bianque.agents.records import DatabaseCallRecorder
from bianque.agents.scripted_rules import ROLE_RULES
from bianque.agents.snapshot import preserved_identifiers
from bianque.api.alertmanager import parse_alert
from bianque.config import Settings
from bianque.gate import FollowupAnswer, GateContext, OutputType
from bianque.knowledge import KnowledgeRetriever
from bianque.knowledge.embedding import build_embedder
from bianque.knowledge.search import KnowledgeSearch, SearchResult
from bianque.llm import (
    CallContext,
    LLMError,
    LLMGateway,
    ScriptedRule,
    build_gateway,
    build_redaction_engine,
    redact_for_llm,
    redact_prompt_for_llm,
)
from bianque.redaction import RedactionEngine
from bianque.storage.errors import NotFoundError
from bianque.storage.models import Alert, FollowupMessage, FollowupSession
from bianque.storage.repositories import (
    CallRecordRepository,
    EvidenceRepository,
    FollowupRepository,
    InvestigationRepository,
    KnowledgeRepository,
    ReportRepository,
    SuggestionRepository,
)
from bianque.tools import registry

logger = logging.getLogger(__name__)

#: The agent name of follow-up calls in call records and request keys.
FOLLOWUP = "followup"

#: The call site of the answer, stored as the model call record name.
ANSWER = "answer"

#: The call record name of the knowledge retrieval for a question.
RETRIEVE_KNOWLEDGE = "retrieve_knowledge"

#: The function the model is forced to call.
ANSWER_FUNCTION = ("submit_answer", "Submit the answer to the follow-up question.")

#: Earlier question and answer pairs given to the model.
HISTORY_TURNS = 10

#: Seconds between progress events while the stage does not change.
PROGRESS_INTERVAL = 2.0

#: The schema version of ``questions.yaml``.
QUESTIONS_SCHEMA_VERSION = 1

FailureReason = Literal["gate_rejected", "model_unavailable", "model_error", "budget_exhausted"]
ProgressStage = Literal["retrieving", "generating", "validating", "retrying"]


@dataclass(frozen=True)
class Progress:
    """The flow is at ``stage``; ``elapsed_ms`` since the question was received."""

    stage: ProgressStage
    elapsed_ms: int


@dataclass(frozen=True)
class Content:
    """One paragraph of an answer that passed the gate."""

    segment_index: int
    text: str
    citations: tuple[str, ...]


@dataclass(frozen=True)
class Done:
    """The answer was stored as ``message_id``; all its paragraphs were sent."""

    message_id: int


@dataclass(frozen=True)
class Failed:
    """No answer: why, and the names of the validators that rejected the last attempt."""

    reason: FailureReason
    violations: tuple[str, ...] = ()


FollowupEvent = Progress | Content | Done | Failed


class NoReportError(RuntimeError):
    """The investigation has no published or degraded report to ask about."""


class SessionBusyError(RuntimeError):
    """The session is still answering an earlier question."""


class QuestionNotRecordedError(ValueError):
    """In ``recorded`` mode, the question is not the next recorded one of the session.

    Attributes:
        expected: The question the session may ask next, or ``None`` when every
            recorded question has been asked or the case has none.
    """

    def __init__(self, expected: str | None) -> None:
        """Remember the question that may be asked next."""
        super().__init__("the question is not the next recorded question of the session")
        self.expected = expected


@dataclass(frozen=True)
class RecordedQuestion:
    """A question of a case's ``questions.yaml``."""

    id: str
    question: str


def load_questions(cases_dir: Path, case_id: str | None) -> list[RecordedQuestion]:
    """Return the follow-up questions of a case, in order; none when it has no file.

    Raises:
        ValueError: The file has another schema version or a malformed question.
    """
    if case_id is None:
        return []
    path = cases_dir / case_id / "questions.yaml"
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    if data.get("schema_version") != QUESTIONS_SCHEMA_VERSION:
        raise ValueError(f"{path}: schema_version must be {QUESTIONS_SCHEMA_VERSION}")
    return [
        RecordedQuestion(id=str(item["id"]), question=str(item["question"]))
        for item in data.get("questions") or []
    ]


@dataclass
class _Run:
    """One question being answered: its events, and the stage the flow is at."""

    started: float = field(default_factory=time.monotonic)
    stage: ProgressStage = "retrieving"
    queue: asyncio.Queue[FollowupEvent] = field(default_factory=asyncio.Queue)

    def elapsed_ms(self) -> int:
        return round((time.monotonic() - self.started) * 1000)

    def progress(self, stage: ProgressStage) -> None:
        self.stage = stage
        self.queue.put_nowait(Progress(stage, self.elapsed_ms()))


@dataclass(frozen=True)
class _Context:
    """What one answer is generated from, read from the database."""

    investigation_id: str
    evidence: tuple[EvidenceView, ...]
    report: dict[str, Any]
    suggestions: tuple[SuggestionView, ...]
    alert_identifiers: tuple[str, ...]
    history: tuple[tuple[str, list[dict[str, Any]]], ...]

    @property
    def report_references(self) -> set[str]:
        """The knowledge IDs in the report's references."""
        return {
            str(item["knowledge_id"])
            for item in self.report.get("references") or []
            if isinstance(item, dict) and item.get("knowledge_id")
        }

    @property
    def preserve(self) -> frozenset[str]:
        return preserved_identifiers(self.evidence, self.alert_identifiers, registry)


class FollowupService:
    """Answers follow-up questions; one per application."""

    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        *,
        rules: Sequence[ScriptedRule] = (),
        model_sleep: Any = asyncio.sleep,
        knowledge: KnowledgeSearch | None = None,
    ) -> None:
        """Answer with ``settings`` on the database of ``sessions``.

        Args:
            settings: Process settings: model mode, budget and cases directory.
            sessions: Database sessions.
            rules: Scripted rules tried before the role rules; tests inject them.
            model_sleep: Waits between model retries.
            knowledge: Runbook retrieval; built from the settings when omitted.
        """
        self._settings = settings
        self._sessions = sessions
        self._engine: RedactionEngine = build_redaction_engine(settings)
        self._recorder = DatabaseCallRecorder(sessions)
        self._gateway: LLMGateway = build_gateway(
            settings,
            recorder=self._recorder,
            scripted_rules=[*rules, *ROLE_RULES],
            sleep=model_sleep,
        )
        self._knowledge = knowledge or KnowledgeRetriever(sessions, build_embedder(settings))
        self._busy: set[int] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def recorded_only(self) -> bool:
        """Only the recorded questions can be answered, in their order."""
        return self._settings.llm_mode == "recorded"

    async def create_session(self, investigation_id: str, *, created_by: str) -> FollowupSession:
        """Open a follow-up session on an investigation that has a report.

        Raises:
            NotFoundError: No such investigation.
            NoReportError: The investigation has no published or degraded report.
        """
        async with self._sessions() as session, session.begin():
            await InvestigationRepository(session).get(investigation_id)
            if await ReportRepository(session).get_final(investigation_id) is None:
                raise NoReportError(f"investigation {investigation_id} has no report")
            return await FollowupRepository(session).create_session(
                investigation_id, created_by=created_by
            )

    async def questions(self, investigation_id: str) -> list[RecordedQuestion]:
        """Return the recorded questions of the investigation's case.

        Raises:
            NotFoundError: No such investigation.
        """
        async with self._sessions() as session:
            investigation = await InvestigationRepository(session).get(investigation_id)
        return load_questions(self._settings.cases_dir, investigation.case_id)

    async def ask(self, session_id: int, question: str) -> AsyncIterator[FollowupEvent]:
        """Start answering ``question`` and return the stream of its events.

        The checks that can refuse the question run before this returns, so that the
        caller can answer with an error instead of a stream.

        Raises:
            NotFoundError: No such session.
            SessionBusyError: The session is still answering an earlier question.
            QuestionNotRecordedError: ``recorded`` mode and not the next recorded question.
        """
        async with self._sessions() as session:
            followup = await FollowupRepository(session).get_session(session_id)
            investigation = await InvestigationRepository(session).get(followup.investigation_id)
            asked = sum(
                1
                for message in await FollowupRepository(session).list_messages(session_id)
                if message.role == "user"
            )
        if self.recorded_only:
            recorded = load_questions(self._settings.cases_dir, investigation.case_id)
            expected = recorded[asked].question if asked < len(recorded) else None
            if expected is None or question.strip() != expected.strip():
                raise QuestionNotRecordedError(expected)
        if session_id in self._busy:
            raise SessionBusyError(f"session {session_id} is answering a question")
        self._busy.add(session_id)
        run = _Run()
        task = asyncio.create_task(self._answer(run, session_id, question))
        self._tasks.add(task)

        def finished(done: asyncio.Task[None]) -> None:
            self._tasks.discard(done)
            self._busy.discard(session_id)

        task.add_done_callback(finished)
        return self._events(run, task)

    async def close(self) -> None:
        """Cancel the questions being answered: the process is shutting down."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _events(self, run: _Run, task: asyncio.Task[None]) -> AsyncIterator[FollowupEvent]:
        """Yield the events of one run; repeat the stage while nothing else happens."""
        while True:
            try:
                event = await asyncio.wait_for(run.queue.get(), PROGRESS_INTERVAL)
            except TimeoutError:
                if task.done():
                    return
                yield Progress(run.stage, run.elapsed_ms())
                continue
            yield event
            if isinstance(event, Done | Failed):
                return

    async def _answer(self, run: _Run, session_id: int, raw_question: str) -> None:
        try:
            await self._flow(run, session_id, raw_question)
        except Exception:
            # The stream ends without a final event; the log has the details.
            logger.exception("follow-up session %s failed", session_id)

    async def _flow(self, run: _Run, session_id: int, raw_question: str) -> None:
        context = await self._load(session_id)
        async with self._sessions() as session:
            used = await CallRecordRepository(session).tokens_of_session(session_id)
        if used >= self._settings.followup_token_budget:
            run.queue.put_nowait(Failed("budget_exhausted"))
            return
        preserve = context.preserve
        question = redact_for_llm(raw_question.strip(), engine=self._engine, preserve=preserve)
        run.progress("retrieving")
        knowledge = await self._retrieve(session_id, question.text, preserve)
        knowledge_texts = await self._knowledge_texts(knowledge)
        prompt = redact_prompt_for_llm(
            self._messages(context, knowledge_texts, question.text),
            engine=self._engine,
            preserve=preserve,
        )
        # The model may list as references the knowledge retrieved for this question and
        # the knowledge the report refers to, which it sees in the report.
        retrieved = {hit.knowledge_id for hit in knowledge.hits} if knowledge is not None else set()
        gate = GateContext(
            evidence_ids=frozenset(item.id for item in context.evidence),
            knowledge_ids=frozenset(retrieved | context.report_references),
            suggestion_ids=frozenset(item.id for item in context.suggestions),
            preserved=preserve,
            engine=self._engine,
        )

        async def stage(name: Stage) -> None:
            run.progress(name)

        run.progress("generating")
        try:
            gated = await generate_gated(
                self._gateway,
                self._recorder,
                prompt,
                context=CallContext(agent=FOLLOWUP, operation=ANSWER, session_id=session_id),
                output_type=OutputType.FOLLOWUP_ANSWER,
                function_name=ANSWER_FUNCTION[0],
                description=ANSWER_FUNCTION[1],
                gate_context=gate,
                preserve=preserve,
                retries=1,
                on_stage=stage,
            )
        except LLMError:
            # A missing recording, a rejected request or a malformed recording: a defect
            # of the configuration or of the recordings, not of the answer.
            logger.exception("follow-up session %s: the model call failed", session_id)
            run.queue.put_nowait(Failed("model_error"))
            return
        answer = gated.output
        if not isinstance(answer, FollowupAnswer):
            last = gated.results[-1] if gated.results else None
            if last is not None and MODEL_UNAVAILABLE in last.violations:
                run.queue.put_nowait(Failed("model_unavailable"))
            else:
                validators = last.validators() if last is not None else []
                run.queue.put_nowait(Failed("gate_rejected", tuple(validators)))
            return
        message = await self._store(session_id, question.text, answer)
        for index, segment in enumerate(answer.segments):
            run.queue.put_nowait(Content(index, segment.text, tuple(segment.citations)))
        run.queue.put_nowait(Done(message.id))

    async def _load(self, session_id: int) -> _Context:
        async with self._sessions() as session:
            followup = await FollowupRepository(session).get_session(session_id)
            investigation_id = followup.investigation_id
            investigation = await InvestigationRepository(session).get(investigation_id)
            report = await ReportRepository(session).get_final(investigation_id)
            if report is None:
                raise NoReportError(f"investigation {investigation_id} has no report")
            evidence = await EvidenceRepository(session).list(investigation_id)
            suggestions = await SuggestionRepository(session).list_with_decisions(investigation_id)
            alert = (
                await session.get(Alert, investigation.alert_id)
                if investigation.alert_id is not None
                else None
            )
            messages = await FollowupRepository(session).list_messages(session_id)
        return _Context(
            investigation_id=investigation_id,
            evidence=tuple(
                EvidenceView(e.id, e.source, e.tool, e.redacted_content, tuple(e.identifiers))
                for e in evidence
            ),
            report=dict(report.body),
            suggestions=tuple(
                SuggestionView(
                    item.suggestion.id,
                    item.suggestion.seq,
                    item.suggestion.risk,
                    item.suggestion.action,
                    item.suggestion.command,
                )
                for item in suggestions
            ),
            alert_identifiers=parse_alert(alert.payload).identifiers if alert else (),
            history=_turns(messages)[-HISTORY_TURNS:],
        )

    async def _retrieve(
        self, session_id: int, query: str, preserve: frozenset[str]
    ) -> SearchResult | None:
        """Retrieve knowledge for the question and record the retrieval as a call."""
        started_at, clock = datetime.now(UTC), time.monotonic()
        result: SearchResult | None = None
        error: str | None = None
        try:
            result = await self._knowledge.search(query)
        except Exception as exc:
            logger.exception("knowledge retrieval failed for follow-up session %s", session_id)
            error = type(exc).__name__
        async with self._sessions() as session, session.begin():
            await CallRecordRepository(session).add(
                agent=FOLLOWUP,
                kind="knowledge",
                name=RETRIEVE_KNOWLEDGE,
                input_summary=query,
                output_summary=None
                if result is None
                else redact_for_llm(
                    summarize_retrieval(result), engine=self._engine, preserve=preserve
                ).text,
                status="ok" if result is not None else "error",
                error=error,
                started_at=started_at,
                latency_ms=round((time.monotonic() - clock) * 1000),
                session_id=session_id,
            )
        return result

    async def _knowledge_texts(self, result: SearchResult | None) -> str:
        if result is None or not result.hits:
            return ""
        async with self._sessions() as session:
            chunks = await KnowledgeRepository(session).get_chunks(
                [hit.knowledge_id for hit in result.hits]
            )
        return render_knowledge_texts(
            (chunk.id, chunk.heading_path, chunk.content) for chunk, _ in chunks
        )

    def _messages(
        self, context: _Context, knowledge_texts: str, question: str
    ) -> list[tuple[Any, str]]:
        """The conversation: role, data sections, earlier turns, the question."""
        sections = [
            section(EVIDENCE, render_evidence(context.evidence)),
            section(REPORT, render_json(context.report)),
            section(SUGGESTIONS, render_suggestions(context.suggestions)),
            section(KNOWLEDGE, knowledge_texts),
        ]
        messages: list[tuple[Any, str]] = [
            ("system", prompt_text("followup")),
            ("user", "\n\n".join(sections)),
        ]
        for asked, segments in context.history:
            messages += [("user", asked), ("assistant", render_answer(segments))]
        messages.append(("user", question))
        return messages

    async def _store(
        self, session_id: int, question: str, answer: FollowupAnswer
    ) -> FollowupMessage:
        """Store the question and its answer together, so neither exists alone."""
        segments = [segment.model_dump(mode="json") for segment in answer.segments]
        citations = list(dict.fromkeys(c for s in answer.segments for c in s.citations))
        async with self._sessions() as session, session.begin():
            repository = FollowupRepository(session)
            await repository.add_message(session_id, role="user", content=question)
            return await repository.add_message(
                session_id,
                role="assistant",
                content="\n\n".join(segment.text for segment in answer.segments),
                segments=segments,
                citations=citations,
                references=list(answer.references),
                unknown=any(segment.insufficient_evidence for segment in answer.segments),
            )


def _turns(messages: Sequence[FollowupMessage]) -> tuple[tuple[str, list[dict[str, Any]]], ...]:
    """Pair each question with the answer stored after it, in order."""
    turns = []
    question: str | None = None
    for message in messages:
        if message.role == "user":
            question = message.content
        elif question is not None:
            turns.append((question, list(message.segments)))
            question = None
    return tuple(turns)


async def session_messages(
    sessions: async_sessionmaker[AsyncSession], session_id: int
) -> tuple[FollowupSession, list[FollowupMessage]]:
    """Return a follow-up session and its messages in order.

    Raises:
        NotFoundError: No such session.
    """
    async with sessions() as session:
        repository = FollowupRepository(session)
        followup = await repository.get_session(session_id)
        return followup, list(await repository.list_messages(session_id))


__all__ = [
    "ANSWER",
    "FOLLOWUP",
    "Content",
    "Done",
    "Failed",
    "FollowupEvent",
    "FollowupService",
    "NoReportError",
    "NotFoundError",
    "Progress",
    "QuestionNotRecordedError",
    "RecordedQuestion",
    "SessionBusyError",
    "load_questions",
    "session_messages",
]
