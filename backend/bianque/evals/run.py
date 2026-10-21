"""The case evaluation: replay every case, ask its questions, measure, check.

``evaluate`` runs in a throwaway database: it imports the runbooks, runs the retrieval
evaluation, then for each case investigates the alert of its evidence pack and asks its
follow-up questions in order in one session. It reads back what the runs stored,
computes the metrics of ``bianque.evals.metrics`` and the hard gates below, and returns
the report.

The hard gates are deterministic. Each case's investigation ends with a report; every
final report and answer passes the evidence gate again, validated by the same
``bianque.gate.validate`` the runtime uses with the context the runtime had,
which the run's last checkpoint holds; every final report also matches the exported
JSON Schema; no registered tool writes; no evidence comes from an agent; no
knowledge ID is cited as evidence.

With ``record`` set, a ``live`` run also records every model response, one directory
per case, and writes the recording manifest once every case has run: the model, the
prompt files and the skills the recordings were made with.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bianque.agents.followup import Done, Failed, FollowupService
from bianque.agents.prompting import prompt_hashes
from bianque.agents.runner import open_checkpointer, run_investigation
from bianque.agents.service import case_alert
from bianque.agents.snapshot import failed_calls, load_snapshot, preserved_identifiers
from bianque.agents.startup import load_skill_catalog
from bianque.api.alertmanager import AlertInput
from bianque.config import Settings
from bianque.config.manifest import RecordingManifest, load_manifest, write_manifest
from bianque.evals import retrieval
from bianque.evals.cases import Case, QuestionSet, case_directories, load_case, load_question_set
from bianque.evals.metrics import (
    Answer,
    CallRow,
    CaseRun,
    EvidenceRow,
    ReportRow,
    case_result,
    final_report,
    summary,
)
from bianque.evals.report import (
    RECORDED_NOTE,
    REPORT_SCHEMA_VERSION,
    EvalReport,
    HardGate,
    LLMContext,
)
from bianque.gate import FollowupAnswer, GateContext, OutputType, validate
from bianque.gate.schemas import json_schema
from bianque.knowledge import KnowledgeRetriever
from bianque.knowledge.embedding import MODEL_NAME, build_embedder, file_sha256
from bianque.knowledge.ingest import ingest
from bianque.llm import build_gateway, build_redaction_engine
from bianque.skills import SkillCatalog
from bianque.storage.db import create_engine, session_factory
from bianque.storage.models import CallRecord, FollowupMessage, Investigation
from bianque.storage.repositories import (
    AssessmentRepository,
    CallRecordRepository,
    EvidenceRepository,
    FollowupRepository,
    InvestigationRepository,
    KnowledgeRepository,
    PlanRepository,
    ReportRepository,
    SuggestionRepository,
)
from bianque.tools import registry
from bianque.tools.backends import open_backends

#: Evidence sources tools and the alert produce; anything else came from an agent.
EVIDENCE_SOURCES = frozenset({"alertmanager", "kubernetes", "git"})

#: A public address the evaluation must not reach when the network is isolated.
_PROBE_ADDRESS = ("1.1.1.1", 443)


class EvaluationError(RuntimeError):
    """The evaluation cannot run; the message says why."""


@dataclass(frozen=True)
class EvalCase:
    """A case directory with its annotation and questions."""

    directory: Path
    case: Case
    questions: QuestionSet


async def _no_wait(_: float) -> None:
    """Tool retries wait for nothing: the waits are outside every prompt."""


def load_cases(cases_dir: Path, only: Sequence[str] = ()) -> list[EvalCase]:
    """Load the cases to evaluate, all of them or those named in ``only``.

    Raises:
        EvaluationError: A named case does not exist, or there is no case.
        CaseFileError: A case file is invalid.
    """
    directories = case_directories(cases_dir)
    names = [d.name for d in directories]
    unknown = sorted(set(only) - set(names))
    if unknown:
        raise EvaluationError(f"no such case under {cases_dir}: {unknown}")
    chosen = [d for d in directories if not only or d.name in only]
    if not chosen:
        raise EvaluationError(f"no case under {cases_dir}")
    return [EvalCase(d, load_case(d), load_question_set(d)) for d in chosen]


def tool_schemas_sha256() -> str:
    """SHA-256 of the canonical JSON of every registered tool's declaration."""
    specs = sorted(
        (
            {"name": d.name, "description": d.description, "parameters": d.spec().parameters}
            for d in registry
        ),
        key=lambda spec: str(spec["name"]),
    )
    canonical = json.dumps(specs, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def network_isolated(timeout: float = 3.0) -> bool:
    """Whether a connection to a public address fails."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(*_PROBE_ADDRESS), timeout)
    except (OSError, TimeoutError):
        return True
    writer.close()
    return False


# -- One case ----------------------------------------------------------------------------


async def _ask_all(
    service: FollowupService, investigation_id: str, item: EvalCase
) -> list[tuple[str, int | None]]:
    """Ask the case's questions in order in one session: outcome and message of each."""
    if not item.questions.questions:
        return []
    session = await service.create_session(investigation_id, created_by="eval")
    outcomes: list[tuple[str, int | None]] = []
    for question in item.questions.questions:
        outcome: tuple[str, int | None] = ("no_answer", None)
        async for event in await service.ask(session.id, question.question):
            if isinstance(event, Done):
                outcome = ("answered", event.message_id)
            elif isinstance(event, Failed):
                outcome = (event.reason, None)
        outcomes.append(outcome)
    return outcomes


def _call_row(record: CallRecord) -> CallRow:
    return CallRow(
        id=record.id,
        agent=record.agent,
        kind=record.kind,
        name=record.name,
        status=record.status,
        error=record.error,
        gate_result=record.gate_result,
        tokens=(record.tokens_in or 0) + (record.tokens_out or 0),
    )


async def _read_back(
    sessions: async_sessionmaker[AsyncSession],
    item: EvalCase,
    investigation_id: str,
    outcomes: Sequence[tuple[str, int | None]],
    duration_s: float,
) -> CaseRun:
    """Read the rows of one case's run."""
    async with sessions() as session:
        investigation = await InvestigationRepository(session).get(investigation_id)
        plans = await PlanRepository(session).list_plans(investigation_id)
        assessments = await AssessmentRepository(session).list(investigation_id)
        evidence = await EvidenceRepository(session).list(investigation_id)
        suggestions = await SuggestionRepository(session).list_with_decisions(investigation_id)
        calls = await CallRecordRepository(session).list_for_investigation(investigation_id)
        reports = await ReportRepository(session).list_attempts(investigation_id)
        sessions_of = [
            s.session.id for s in await FollowupRepository(session).list_sessions(investigation_id)
        ]
        followup_calls = (
            await session.scalars(
                select(CallRecord)
                .where(CallRecord.session_id.in_(sessions_of))
                .order_by(CallRecord.id)
            )
        ).all()
        messages = {
            m.id: m
            for m in (
                await session.scalars(
                    select(FollowupMessage).where(FollowupMessage.session_id.in_(sessions_of))
                )
            ).all()
        }
    answers = []
    for question, (outcome, message_id) in zip(item.questions.questions, outcomes, strict=True):
        message = messages.get(message_id) if message_id is not None else None
        answers.append(
            Answer(
                question=question,
                outcome=outcome,
                segments=tuple(message.segments) if message else (),
                references=tuple(message.references) if message else (),
                unknown=message.unknown if message else False,
            )
        )
    return CaseRun(
        case=item.case,
        investigation_id=investigation_id,
        status=investigation.status,
        error=investigation.error,
        duration_s=duration_s,
        tokens=investigation.tokens_used,
        token_budget=investigation.token_budget,
        plan_sources=[f"v{p.version} ({p.source})" for p in plans],
        rule_overrides=sum(1 for a in assessments if a.rule_override),
        evidence=[
            EvidenceRow(e.id, e.source, e.tool, dict(e.meta.get("arguments") or {}))
            for e in evidence
        ],
        suggestion_ids=frozenset(s.suggestion.id for s in suggestions),
        calls=[_call_row(c) for c in calls],
        followup_calls=[_call_row(c) for c in followup_calls],
        reports=[ReportRow(r.attempt, r.status, dict(r.body)) for r in reports],
        answers=answers,
    )


# -- Hard gates ---------------------------------------------------------------------------


async def _final_state(settings: Settings, investigation_id: str) -> dict[str, Any]:
    """The values of the run's last checkpoint: failures and retrieved knowledge."""
    async with open_checkpointer(settings) as saver:
        saved = await saver.aget_tuple({"configurable": {"thread_id": investigation_id}})
    return dict(saved.checkpoint["channel_values"]) if saved is not None else {}


async def revalidate(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    run: CaseRun,
    alert: AlertInput,
    skills: SkillCatalog,
) -> list[str]:
    """Validate the final report and every answer again; return what failed.

    The report gets the context the runtime gave it: the evidence, the identifiers, the
    failed calls, the knowledge of the run's last checkpoint and the skill names. The
    knowledge retrieved for one follow-up question is not stored, so an answer's
    references are checked against every imported knowledge ID.
    """
    problems: list[str] = []
    engine = build_redaction_engine(settings)
    snapshot = await load_snapshot(sessions, run.investigation_id)
    preserved = preserved_identifiers(snapshot.evidence, alert.identifiers, registry)
    final = final_report(run)
    if final is None:
        return [f"{run.case.case}: no final report"]
    state = await _final_state(settings, run.investigation_id)
    context = GateContext(
        evidence_ids=snapshot.evidence_ids,
        knowledge_ids=frozenset(str(k["id"]) for k in state.get("knowledge") or []),
        preserved=preserved,
        failed_calls=failed_calls(state.get("failures") or []),
        engine=engine,
        skill_names=frozenset(skills.names),
    )
    result = validate(OutputType.REPORT, dict(final.body), context)
    if not result.passed:
        problems.append(f"{run.case.case}: final report rejected by {result.validators()}")
    try:
        jsonschema.validate(dict(final.body), json_schema(OutputType.REPORT))
    except jsonschema.ValidationError as exc:
        problems.append(f"{run.case.case}: final report breaks the JSON Schema: {exc.message}")
    async with sessions() as session:
        known = {chunk.id for chunk, _ in await KnowledgeRepository(session).all_chunks()}
    answer_context = GateContext(
        evidence_ids=snapshot.evidence_ids,
        knowledge_ids=frozenset(known),
        suggestion_ids=run.suggestion_ids,
        preserved=preserved,
        engine=engine,
        skill_names=frozenset(skills.names),
    )
    for answer in run.answers:
        if answer.outcome != "answered":
            continue
        body = {"segments": list(answer.segments), "references": list(answer.references)}
        checked = validate(OutputType.FOLLOWUP_ANSWER, body, answer_context)
        if not checked.passed or not isinstance(checked.output, FollowupAnswer):
            problems.append(
                f"{run.case.case} {answer.question.id}: answer rejected by {checked.validators()}"
            )
    return problems


def _citations_of(run: CaseRun) -> set[str]:
    final = final_report(run)
    cited: set[str] = set()
    if final is not None:
        body = final.body
        for key in ("timeline", "claims", "suggestions"):
            for item in body.get(key) or []:
                cited.update(item.get("citations") or [])
        cited.update((body.get("root_cause") or {}).get("citations") or [])
    for answer in run.answers:
        for segment in answer.segments:
            cited.update(segment.get("citations") or [])
    return cited


def hard_gates(
    runs: Sequence[CaseRun], revalidation: Sequence[str], knowledge_ids: set[str]
) -> list[HardGate]:
    """The deterministic checks of the evaluation."""
    unfinished = [
        f"{run.case.case}: {run.status}" + (f" ({run.error})" if run.error else "")
        for run in runs
        if run.status not in ("reported", "degraded")
    ]
    writers = sorted(d.name for d in registry if not d.read_only)
    agent_evidence = [
        f"{run.case.case}: {e.id} from {e.source}"
        for run in runs
        for e in run.evidence
        if e.source not in EVIDENCE_SOURCES
    ]
    knowledge_cited = sorted(
        f"{run.case.case}: {kid}" for run in runs for kid in _citations_of(run) & knowledge_ids
    )
    return [
        HardGate(name="investigations_report", passed=not unfinished, details=unfinished),
        HardGate(name="outputs_pass_the_gate", passed=not revalidation, details=list(revalidation)),
        HardGate(name="no_write_tools", passed=not writers, details=writers),
        HardGate(name="no_agent_evidence", passed=not agent_evidence, details=agent_evidence),
        HardGate(name="knowledge_not_cited", passed=not knowledge_cited, details=knowledge_cited),
    ]


# -- The evaluation -----------------------------------------------------------------------


async def evaluate(
    settings: Settings,
    cases: Sequence[EvalCase],
    *,
    queries: Path,
    record: Path | None = None,
    check_network: bool = False,
) -> EvalReport:
    """Evaluate ``cases`` and return the report.

    Args:
        settings: Process settings: model mode, embeddings, database, cases directory.
        cases: The cases, from ``load_cases``.
        queries: The annotated retrieval queries.
        record: ``live`` mode only: record every response into ``<record>/<case>/`` and
            write the recording manifest there.
        check_network: Measure whether the process can reach a public address; the
            evaluation fails when it can.

    Raises:
        EvaluationError: The database is not empty, or ``record`` without ``live`` mode.
    """
    if record is not None and settings.llm_mode != "live":
        raise EvaluationError("recording needs live model mode")
    if record is not None:
        settings = settings.model_copy(update={"llm_record_dir": record})
    skills = load_skill_catalog(settings)
    isolated = await network_isolated() if check_network else None
    engine = create_engine(settings)
    try:
        sessions = session_factory(engine)
        async with sessions() as session:
            if await session.scalar(select(func.count()).select_from(Investigation)):
                raise EvaluationError(
                    "the database already holds investigations; evaluate in an empty one"
                )
        embedder = build_embedder(settings)
        await ingest(settings.knowledge_dir, sessions=sessions, embedder=embedder)
        retrieval_report = await _retrieval(settings, sessions, queries)
        followups = FollowupService(settings, sessions, skills=skills)
        runs: list[CaseRun] = []
        problems: list[str] = []
        for item in cases:
            alert = case_alert(settings, item.case.case)
            started = time.monotonic()
            async with open_backends(settings, case=item.case.case) as backends:
                outcome = await run_investigation(
                    settings,
                    alert,
                    sessions=sessions,
                    backends=backends,
                    skills=skills,
                    trigger="replay",
                    case_id=item.case.case,
                    tool_sleep=_no_wait,
                )
            answered = (
                await _ask_all(followups, outcome.investigation_id, item)
                if outcome.status != "failed"
                else [("no_report", None)] * len(item.questions.questions)
            )
            run = await _read_back(
                sessions, item, outcome.investigation_id, answered, time.monotonic() - started
            )
            runs.append(run)
            if run.status != "failed":
                problems += await revalidate(settings, sessions, run, alert, skills)
        await followups.close()
        async with sessions() as session:
            knowledge_ids = {
                chunk.id for chunk, _ in await KnowledgeRepository(session).all_chunks()
            }
    finally:
        await engine.dispose()
    gates = hard_gates(runs, problems, knowledge_ids)
    if isolated is not None:
        gates.append(
            HardGate(
                name="network_isolated",
                passed=isolated,
                details=[] if isolated else [f"reached {_PROBE_ADDRESS[0]}:{_PROBE_ADDRESS[1]}"],
            )
        )
    results = [case_result(run, report_attempts=1 + settings.report_max_retries) for run in runs]
    manifest = (
        load_manifest(settings.llm_recordings_dir) if settings.llm_mode == "recorded" else None
    )
    report = EvalReport(
        schema_version=REPORT_SCHEMA_VERSION,
        generated_at=datetime.now(UTC).replace(microsecond=0),
        passed=all(gate.passed for gate in gates),
        llm=LLMContext(
            mode=settings.llm_mode,
            model=settings.llm_model,
            extra_body=settings.llm_extra_body,
            recorded_at=manifest.recorded_at if manifest else None,
            note=RECORDED_NOTE if settings.llm_mode == "recorded" else None,
        ),
        embedding=_embedding_context(settings),
        prompts=prompt_hashes(),
        tool_schemas_sha256=tool_schemas_sha256(),
        network_isolated=isolated,
        hard_gates=gates,
        summary=summary(results),
        cases=results,
        retrieval=retrieval_report,
    )
    if record is not None and report.passed:
        write_manifest(
            record,
            RecordingManifest(
                schema_version=2,
                model=settings.llm_model,
                extra_body=settings.llm_extra_body,
                endpoint_host=build_gateway(settings).endpoint_host,
                recorded_at=report.generated_at,
                cases=tuple(item.case.case for item in cases),
                prompts=report.prompts,
                skills=skills.hashes(),
                tool_schemas_sha256=report.tool_schemas_sha256,
            ),
        )
    return report


def _embedding_context(settings: Settings) -> dict[str, str]:
    context = {"mode": settings.embedding_mode, "model": MODEL_NAME}
    if settings.embedding_mode == "fixture":
        context["fixture_file"] = settings.embedding_fixture.name
        context["fixture_sha256"] = file_sha256(settings.embedding_fixture)
    return context


async def _retrieval(
    settings: Settings, sessions: async_sessionmaker[AsyncSession], queries: Path
) -> dict[str, Any]:
    """The retrieval evaluation on the imported runbooks."""
    annotated = retrieval.load_queries(queries)
    async with sessions() as session:
        known = {chunk.id for chunk, _ in await KnowledgeRepository(session).all_chunks()}
    retrieval.check_annotations(annotated, known, queries)
    outcomes = await retrieval.evaluate(
        annotated, KnowledgeRetriever(sessions, build_embedder(settings))
    )
    return {
        "queries_file": queries.name,
        "queries_sha256": file_sha256(queries),
        "top": retrieval.TOP,
        "queries": len(outcomes),
        "metrics": {name: round(v, 4) for name, v in retrieval.metrics(outcomes).items()},
        "outcomes": [
            {"id": o.id, "top": o.top, "first_hit_rank": o.first_hit_rank} for o in outcomes
        ],
    }
