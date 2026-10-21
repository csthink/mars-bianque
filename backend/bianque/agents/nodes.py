"""The nodes of the investigation graph, except the evidence sub-agent.

Every node reads what it needs from the database and commits its own writes, so the
investigation is visible while it runs. Each model output passes the evidence gate
before anything is stored; a plan, an assessment or a report that the gate rejects
never reaches a business table or an event.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from langgraph.types import Send

from bianque.agents.default_plan import default_plan
from bianque.agents.domains import DOMAINS, POD_STATUS_TOOLS
from bianque.agents.gated import (
    MODEL_UNAVAILABLE,
    SCHEMA_FAILURE,
    generate_gated,
    rejection_feedback,
)
from bianque.agents.prompting import (
    ASSESSMENT,
    DOMAINS_HEADING,
    EVIDENCE,
    FAILED_CALLS,
    FINDINGS,
    KNOWLEDGE,
    REJECTION,
    SKILLS,
    build_prompt,
    prompt_text,
    render_domains,
    render_evidence,
    render_failures,
    render_findings,
    render_json,
    render_skills,
    render_violations,
    section,
    skills_prompt,
)
from bianque.agents.skill_loop import SkillLoop
from bianque.agents.snapshot import (
    Snapshot,
    failed_calls,
    failure_views,
    gate_context,
    load_snapshot,
    preserved_identifiers,
)
from bianque.agents.state import AgentTask, InvestigationState, PendingStep, Runtime
from bianque.gate import (
    SCHEMA_VERSION,
    Assessment,
    GateContext,
    GateResult,
    OutputType,
    Plan,
    PlanStep,
    Report,
    Violation,
    build_degraded_report,
    validate,
)
from bianque.knowledge.query import alert_query
from bianque.knowledge.search import SearchResult
from bianque.llm import (
    CallContext,
    LLMUnavailableError,
    RedactedPrompt,
    StructuredOutputError,
    ToolCall,
    ToolSpec,
    redact_for_llm,
)
from bianque.llm.gateway import function_parameters
from bianque.storage.ids import alert_evidence_id
from bianque.storage.repositories import (
    AssessmentRepository,
    CallRecordRepository,
    EvidenceRepository,
    InvestigationRepository,
    KnowledgeRepository,
    PlanRepository,
    ReportRepository,
    StepSpec,
    SuggestionRepository,
    SuggestionSpec,
)

logger = logging.getLogger(__name__)

#: The agent name of the investigation agent in call records.
INVESTIGATION = "investigation"

#: The name of the knowledge retrieval in call records.
RETRIEVE_KNOWLEDGE = "retrieve_knowledge"

#: Appended to the reason of the assessment after which the budget stopped the rounds.
BUDGET_EXHAUSTED = "Token budget exhausted."

#: The reason of an assessment decided by the rule alone.
RULE_REASON = (
    "The model's assessment did not pass the evidence gate; the deterministic rule decided."
)

#: The descriptions of the functions the investigation agent is forced to call.
PLAN_FUNCTION = ("submit_plan", "Submit the investigation plan.")
ASSESS_FUNCTION = ("submit_assessment", "Submit whether the evidence suffices for a report.")
REPORT_FUNCTION = ("submit_report", "Submit the investigation report.")


class InvestigationError(RuntimeError):
    """The investigation cannot continue; it ends as ``failed``."""


def _now() -> datetime:
    return datetime.now(UTC)


def check_range(plan: Plan, *, first_round: bool) -> tuple[list[PlanStep], list[dict[str, Any]]]:
    """Split a plan's steps into those the round allows and those it drops.

    A step is dropped when its domain is not declared, or when the first round uses a
    domain that is only allowed later.

    Returns:
        The kept steps and, for each dropped step, its position and domain.
    """
    allowed = {item.name for item in DOMAINS if item.first_round or not first_round}
    kept: list[PlanStep] = []
    dropped: list[dict[str, Any]] = []
    for index, step in enumerate(plan.steps):
        if step.domain in allowed:
            kept.append(step)
        else:
            dropped.append({"index": index, "domain": step.domain})
    return kept, dropped


def render_knowledge(knowledge: Iterable[dict[str, str]]) -> str:
    """Render the knowledge given to the model by ID and title, sorted by ID."""
    return "\n".join(f"- {k['id']}: {k['title']}" for k in sorted(knowledge, key=lambda k: k["id"]))


def render_knowledge_texts(chunks: Iterable[tuple[str, str, str]]) -> str:
    """Render runbook chunks as ``[id] title`` followed by the text indented by two spaces.

    The indentation keeps every line of a chunk from reading as a section heading of
    the prompt. Chunks are sorted by ID.
    """
    blocks = []
    for knowledge_id, title, content in sorted(chunks):
        body = "\n".join(f"  {line}" if line.strip() else "" for line in content.splitlines())
        blocks.append(f"[{knowledge_id}] {title}\n{body}")
    return "\n\n".join(blocks)


def summarize_retrieval(result: SearchResult) -> str:
    """The call record's output summary of a retrieval: IDs, titles, ranks and scores."""
    lines = [f"degraded: {result.degraded}"] if result.degraded else []
    lines += [
        f"{hit.knowledge_id} {hit.heading_path} (vector rank {hit.vector_rank or '-'}, "
        f"text rank {hit.text_rank or '-'}, score {hit.score:.6f})"
        for hit in result.hits
    ]
    return "\n".join(lines) or "(no knowledge found)"


class InvestigationNodes:
    """The nodes and routing functions of the investigation graph."""

    def __init__(self, runtime: Runtime) -> None:
        """Bind the nodes to the runtime of one investigation run."""
        self._rt = runtime

    def _context(self, investigation_id: str, operation: str) -> CallContext:
        return CallContext(
            agent=INVESTIGATION, operation=operation, investigation_id=investigation_id
        )

    def _preserve(self, snapshot: Snapshot) -> frozenset[str]:
        rt = self._rt
        return preserved_identifiers(snapshot.evidence, rt.alert_identifiers, rt.tools)

    def _common_sections(self, snapshot: Snapshot, state: InvestigationState) -> list[str]:
        return [
            section(EVIDENCE, render_evidence(snapshot.evidence)),
            section(FINDINGS, render_findings(snapshot.findings)),
            section(FAILED_CALLS, render_failures(failure_views(state.get("failures", [])))),
        ]

    # Nodes ------------------------------------------------------------------------------

    async def intake(self, state: InvestigationState) -> dict[str, Any]:
        """Start the investigation and store the alert as its first evidence."""
        rt = self._rt
        investigation_id = state["investigation_id"]
        alert = rt.alert
        redacted = redact_for_llm(alert.text, engine=rt.engine, preserve=alert.identifiers)
        evidence_id = alert_evidence_id(alert.fingerprint, redacted.text)
        async with rt.sessions() as session, session.begin():
            investigations = InvestigationRepository(session)
            await investigations.set_status(investigation_id, "running")
            trigger = (await investigations.get(investigation_id)).trigger
            await EvidenceRepository(session).add(
                investigation_id,
                evidence_id=evidence_id,
                source="alertmanager",
                collected_at=_now(),
                raw_content=alert.text,
                redacted_content=redacted.text,
                identifiers=alert.identifiers,
                meta={"fingerprint": alert.fingerprint},
            )
        await rt.events.emit(investigation_id, "investigation_started", trigger=trigger)
        rt.announced.add(evidence_id)
        await rt.events.emit(
            investigation_id,
            "evidence_added",
            evidence_id=evidence_id,
            source="alertmanager",
            tool=None,
        )
        return {
            "round": 1,
            "failures": [],
            "knowledge": [],
            "skills": [],
            "report_attempt": 0,
            "rejection": [],
            "model_failed": False,
        }

    async def retrieve_knowledge(self, state: InvestigationState) -> dict[str, Any]:
        """Retrieve runbook knowledge for the alert and record the retrieval as a call.

        Knowledge is a reference, not a precondition: when retrieval fails, the
        investigation continues without knowledge and the call record says why.
        """
        rt = self._rt
        investigation_id = state["investigation_id"]
        query = alert_query(rt.alert.labels, rt.alert.annotations)
        started_at, clock = _now(), time.monotonic()
        result: SearchResult | None = None
        error: str | None = None
        try:
            result = await rt.knowledge.search(query)
        except Exception as exc:
            logger.exception("knowledge retrieval failed for investigation %s", investigation_id)
            error = type(exc).__name__
        latency_ms = round((time.monotonic() - clock) * 1000)
        preserve = rt.alert_identifiers
        async with rt.sessions() as session, session.begin():
            await CallRecordRepository(session).add(
                agent=INVESTIGATION,
                kind="knowledge",
                name=RETRIEVE_KNOWLEDGE,
                input_summary=redact_for_llm(query, engine=rt.engine, preserve=preserve).text,
                output_summary=None
                if result is None
                else redact_for_llm(
                    summarize_retrieval(result), engine=rt.engine, preserve=preserve
                ).text,
                status="ok" if result is not None else "error",
                error=error,
                started_at=started_at,
                latency_ms=latency_ms,
                investigation_id=investigation_id,
            )
        hits = result.hits if result is not None else []
        return {"knowledge": [{"id": hit.knowledge_id, "title": hit.heading_path} for hit in hits]}

    async def _knowledge_texts(self, state: InvestigationState) -> str:
        """Render the investigation's knowledge with the chunk texts, read by ID."""
        ids = [k["id"] for k in state.get("knowledge", [])]
        if not ids:
            return ""
        async with self._rt.sessions() as session:
            chunks = await KnowledgeRepository(session).get_chunks(ids)
        return render_knowledge_texts(
            (chunk.id, chunk.heading_path, chunk.content) for chunk, _ in chunks
        )

    async def plan(self, state: InvestigationState) -> dict[str, Any]:
        """Generate the first plan: steps of the first-round domains only."""
        rt = self._rt
        snapshot = await load_snapshot(rt.sessions, state["investigation_id"])
        sections = [
            section(EVIDENCE, render_evidence(snapshot.evidence)),
            section(DOMAINS_HEADING, render_domains(rt.tools, first_round=True)),
            section(KNOWLEDGE, await self._knowledge_texts(state)),
        ]
        pending, loaded = await self._make_plan(state, snapshot, sections, "plan", first_round=True)
        return {"pending": pending, "skills": loaded}

    async def replan(self, state: InvestigationState) -> dict[str, Any]:
        """Generate a supplementary plan for what the last assessment found missing."""
        rt = self._rt
        investigation_id = state["investigation_id"]
        snapshot = await load_snapshot(rt.sessions, investigation_id)
        async with rt.sessions() as session:
            latest = (await AssessmentRepository(session).list(investigation_id))[-1]
        assessment = {
            "reason": latest.reason,
            "hypothesis_category": latest.hypothesis_category,
            "missing": latest.missing,
        }
        sections = [
            *self._common_sections(snapshot, state),
            section(ASSESSMENT, render_json(assessment)),
            section(DOMAINS_HEADING, render_domains(rt.tools, first_round=False)),
            section(KNOWLEDGE, await self._knowledge_texts(state)),
        ]
        if rt.plans_with_skills:
            sections.append(section(SKILLS, render_skills(rt.skills, state.get("skills", []))))
        pending, loaded = await self._make_plan(
            state, snapshot, sections, "replan", first_round=False
        )
        return {"pending": pending, "skills": loaded, "round": state["round"] + 1}

    async def _make_plan(
        self,
        state: InvestigationState,
        snapshot: Snapshot,
        sections: Sequence[str],
        operation: str,
        *,
        first_round: bool,
    ) -> tuple[list[PendingStep], list[str]]:
        """Generate, check and store a plan version; return its steps and the loaded skills.

        With skills, the model may load some first (``SkillLoop``); a plan it submits
        there is validated like a forced one. Without skills, the plan is one forced call.
        """
        rt = self._rt
        investigation_id = state["investigation_id"]
        preserve = self._preserve(snapshot)
        knowledge_ids = [k["id"] for k in state.get("knowledge", [])]
        context = self._context(investigation_id, operation)
        skills = skills_prompt(rt.skills) if rt.plans_with_skills else ""
        prompt = build_prompt(
            prompt_text(operation, skills=skills), sections, engine=rt.engine, preserve=preserve
        )
        loaded = list(state.get("skills", []))
        submitted: tuple[ToolCall, str] | None = None
        unavailable = False
        if rt.plans_with_skills:
            outcome = await SkillLoop(rt, self._plan_function).run(
                prompt, context, loaded, preserve
            )
            prompt, submitted = outcome.conversation, outcome.submitted
            loaded, unavailable = list(outcome.loaded), outcome.unavailable
        plan: Plan | None = None
        result: GateResult | None = None
        if not unavailable:
            plan, result = await self._gated_plan(
                prompt,
                context,
                submitted,
                gate_context(snapshot, rt, knowledge_ids=knowledge_ids),
                preserve,
            )
        source = "model"
        if plan is not None and result is not None:
            kept, dropped = check_range(plan, first_round=first_round)
            if dropped:
                await rt.recorder.set_gate_result(
                    context, {**result.to_json(), "dropped_steps": dropped}
                )
            plan = plan.model_copy(update={"steps": kept}) if kept else None
        if plan is None:
            plan, source = default_plan(first_round=first_round), "default"
        async with rt.sessions() as session, session.begin():
            row = await PlanRepository(session).add_plan(
                investigation_id,
                source=source,
                rationale=plan.rationale,
                skills=sorted(loaded),
                steps=[
                    StepSpec(
                        domain=step.domain,
                        goal=step.goal,
                        agent=step.domain,
                        suggested_tools=step.suggested_tools,
                        expected_evidence=[step.expected_evidence],
                    )
                    for step in plan.steps
                ],
            )
            steps = await PlanRepository(session).list_steps(investigation_id)
        await rt.events.emit(
            investigation_id, "plan_created", plan_id=row.id, version=row.version, source=source
        )
        pending = [PendingStep(id=s.id, domain=s.domain) for s in steps if s.plan_id == row.id]
        return pending, loaded

    @property
    def _plan_function(self) -> ToolSpec:
        return ToolSpec(PLAN_FUNCTION[0], PLAN_FUNCTION[1], function_parameters(Plan))

    async def _gated_plan(
        self,
        prompt: RedactedPrompt,
        context: CallContext,
        submitted: tuple[ToolCall, str] | None,
        gate: GateContext,
        preserve: frozenset[str],
    ) -> tuple[Plan | None, GateResult | None]:
        """Validate a plan the model submitted on its own, or force one; one retry either way.

        Returns:
            The plan that passed the gate, or ``None``, and the last gate result.
        """
        rt = self._rt
        retries = 1
        if submitted is not None:
            call, text = submitted
            try:
                arguments: Any = json.loads(call.arguments)
            except json.JSONDecodeError:
                result = GateResult(OutputType.PLAN.value, (SCHEMA_FAILURE,))
            else:
                result = validate(OutputType.PLAN, arguments, gate)
            await rt.recorder.set_gate_result(context, result.to_json())
            if result.passed and isinstance(result.output, Plan):
                return result.output, result
            prompt = prompt + rejection_feedback(
                call, text, result.violations, engine=rt.engine, preserve=preserve
            )
            retries = 0
        gated = await generate_gated(
            rt.gateway,
            rt.recorder,
            prompt,
            context=context,
            output_type=OutputType.PLAN,
            function_name=PLAN_FUNCTION[0],
            description=PLAN_FUNCTION[1],
            gate_context=gate,
            preserve=preserve,
            retries=retries,
        )
        plan = gated.output if isinstance(gated.output, Plan) else None
        return plan, gated.results[-1] if gated.results else None

    async def dispatch(self, state: InvestigationState) -> dict[str, Any]:
        """Mark the start of a round; the routing function sends the steps out."""
        return {}

    def route_steps(self, state: InvestigationState) -> list[Send] | str:
        """Send each domain's steps of this round to its sub-agent, all in parallel."""
        tasks = []
        for item in DOMAINS:
            step_ids = [
                step["id"] for step in state.get("pending", []) if step["domain"] == item.name
            ]
            if step_ids:
                task = AgentTask(
                    investigation_id=state["investigation_id"], domain=item.name, step_ids=step_ids
                )
                tasks.append(Send("evidence_agent", task))
        return tasks or "assess"

    async def assess(self, state: InvestigationState) -> dict[str, Any]:
        """Judge whether the evidence suffices, apply the rule, then check the budget."""
        rt = self._rt
        investigation_id = state["investigation_id"]
        snapshot = await load_snapshot(rt.sessions, investigation_id)
        preserve = self._preserve(snapshot)
        gated = await generate_gated(
            rt.gateway,
            rt.recorder,
            build_prompt(
                prompt_text("assess"),
                self._common_sections(snapshot, state),
                engine=rt.engine,
                preserve=preserve,
            ),
            context=self._context(investigation_id, "assess"),
            output_type=OutputType.ASSESSMENT,
            function_name=ASSESS_FUNCTION[0],
            description=ASSESS_FUNCTION[1],
            gate_context=gate_context(snapshot, rt, failures=state.get("failures", [])),
            preserve=preserve,
        )
        judged = gated.output if isinstance(gated.output, Assessment) else None
        category = judged.hypothesis_category if judged else None
        rule_missing = self._rule_missing(snapshot, category)
        if judged is not None:
            sufficient = judged.sufficient and not rule_missing
            override = judged.sufficient and bool(rule_missing)
            reason = judged.reason
            missing = [*judged.missing, *(m for m in rule_missing if m not in judged.missing)]
        else:
            sufficient, override, reason, missing = (
                not rule_missing,
                True,
                RULE_REASON,
                rule_missing,
            )
        async with rt.sessions() as session:
            used = (await InvestigationRepository(session).get(investigation_id)).tokens_used
        exhausted = used > rt.token_budget
        if exhausted:
            reason = f"{reason} {BUDGET_EXHAUSTED}"
        async with rt.sessions() as session, session.begin():
            await AssessmentRepository(session).add(
                investigation_id,
                round=state["round"],
                sufficient=sufficient,
                reason=reason,
                hypothesis_category=category,
                missing=missing,
                rule_override=override,
            )
        await rt.events.emit(
            investigation_id,
            "assessment_recorded",
            round=state["round"],
            sufficient=sufficient,
            rule_override=override,
        )
        return {"sufficient": sufficient, "budget_exhausted": exhausted}

    def _rule_missing(self, snapshot: Snapshot, category: str | None) -> list[str]:
        """Return what the deterministic rule requires and the evidence lacks."""
        used = snapshot.tools_used()
        domains_with_evidence = {
            definition.domain
            for tool in used
            if (definition := self._rt.tools.get(tool)) is not None
        }
        missing = []
        if not used & set(POD_STATUS_TOOLS):
            missing.append(f"Pod status evidence ({', '.join(POD_STATUS_TOOLS)}).")
        missing += [
            f"Evidence of the {item.name} domain."
            for item in DOMAINS
            if category in item.required_for_categories and item.name not in domains_with_evidence
        ]
        return missing

    def after_assess(self, state: InvestigationState) -> str:
        """Report when the evidence suffices, the budget is used up or no round is left."""
        if state.get("sufficient") or state.get("budget_exhausted"):
            return "report"
        return "report" if state["round"] > self._rt.max_replan_rounds else "replan"

    async def report(self, state: InvestigationState) -> dict[str, Any]:
        """Generate a report from all evidence; after a rejection, show the violations."""
        rt = self._rt
        investigation_id = state["investigation_id"]
        snapshot = await load_snapshot(rt.sessions, investigation_id)
        preserve = self._preserve(snapshot)
        sections = [
            *self._common_sections(snapshot, state),
            section(KNOWLEDGE, render_knowledge(state.get("knowledge", []))),
        ]
        rejection = [Violation(**v) for v in state.get("rejection", [])]
        if rejection:
            sections.append(
                section(
                    REJECTION,
                    "The evidence gate rejected your previous report:\n"
                    f"{render_violations(rejection)}\n"
                    "Write the report again so that it passes these checks.",
                )
            )
        attempt = state.get("report_attempt", 0) + 1
        try:
            structured = await rt.gateway.complete_structured(
                build_prompt(prompt_text("report"), sections, engine=rt.engine, preserve=preserve),
                context=self._context(investigation_id, "report"),
                output=Report,
                function_name=REPORT_FUNCTION[0],
                description=REPORT_FUNCTION[1],
            )
        except StructuredOutputError:
            return {"report_attempt": attempt, "report_body": None}
        except LLMUnavailableError:
            # The degraded report names every validator that rejected an earlier attempt.
            return {
                "report_attempt": attempt,
                "report_body": None,
                "model_failed": True,
                "rejection": [*state.get("rejection", []), MODEL_UNAVAILABLE.to_json()],
            }
        return {"report_attempt": attempt, "report_body": structured.value.model_dump(mode="json")}

    def after_report(self, state: InvestigationState) -> str:
        """Validate the report, or degrade when the model could not be called."""
        return "degrade" if state.get("model_failed") else "gate"

    async def gate(self, state: InvestigationState) -> dict[str, Any]:
        """Validate the report; store a rejected attempt and decide whether to retry."""
        rt = self._rt
        investigation_id = state["investigation_id"]
        body = state.get("report_body")
        snapshot = await load_snapshot(rt.sessions, investigation_id)
        context = gate_context(
            snapshot,
            rt,
            failures=state.get("failures", []),
            knowledge_ids=[k["id"] for k in state.get("knowledge", [])],
        )
        result = (
            validate(OutputType.REPORT, body, context)
            if body is not None
            else GateResult(OutputType.REPORT.value, (SCHEMA_FAILURE,))
        )
        await rt.recorder.set_gate_result(
            self._context(investigation_id, "report"), result.to_json()
        )
        if result.passed and result.output is not None:
            return {
                "verdict": "passed",
                "report_body": result.output.model_dump(mode="json"),
                "report_gate": result.to_json(),
            }
        async with rt.sessions() as session, session.begin():
            row = await ReportRepository(session).add_attempt(
                investigation_id,
                status="rejected",
                body=body or {},
                schema_version=SCHEMA_VERSION,
                gate_result=result.to_json(),
            )
        await rt.events.emit(
            investigation_id, "report_rejected", attempt=row.attempt, violations=result.validators()
        )
        retry = state.get("report_attempt", 0) <= rt.report_max_retries
        return {
            "verdict": "retry" if retry else "degrade",
            "rejection": [v.to_json() for v in result.violations],
        }

    def after_gate(self, state: InvestigationState) -> str:
        """Publish a passed report, retry a rejected one, or degrade."""
        return {"passed": "publish", "retry": "report", "degrade": "degrade"}[state["verdict"]]

    async def degrade(self, state: InvestigationState) -> dict[str, Any]:
        """Build the degraded report from the evidence and the deterministic rules."""
        rt = self._rt
        investigation_id = state["investigation_id"]
        snapshot = await load_snapshot(rt.sessions, investigation_id)
        failures = state.get("failures", [])
        rejected_by = sorted({v["validator"] for v in state.get("rejection", [])})
        report = build_degraded_report(snapshot.items, failed_calls(failures), rejected_by)
        result = validate(OutputType.REPORT, report, gate_context(snapshot, rt, failures=failures))
        if not result.passed:
            raise InvestigationError(
                f"the degraded report did not pass the gate: {result.validators()}"
            )
        return {
            "verdict": "degrade",
            "report_body": report.model_dump(mode="json"),
            "report_gate": result.to_json(),
        }

    async def publish(self, state: InvestigationState) -> dict[str, Any]:
        """Store the final report and its suggestions, and end the investigation."""
        rt = self._rt
        investigation_id = state["investigation_id"]
        body = state.get("report_body") or {}
        degraded = state.get("verdict") == "degrade"
        status = "degraded" if degraded else "reported"
        async with rt.sessions() as session, session.begin():
            row = await ReportRepository(session).add_attempt(
                investigation_id,
                status="degraded" if degraded else "published",
                body=body,
                schema_version=SCHEMA_VERSION,
                gate_result=state.get("report_gate"),
            )
            if not degraded:
                await SuggestionRepository(session).add_all(
                    investigation_id,
                    row.id,
                    [
                        SuggestionSpec(
                            action=s["action"],
                            risk=s["risk"],
                            command=s.get("command"),
                            citations=s["citations"],
                        )
                        for s in body.get("suggestions", [])
                    ],
                )
            await InvestigationRepository(session).set_status(
                investigation_id, status, finished_at=_now()
            )
        await rt.events.emit(
            investigation_id,
            "report_degraded" if degraded else "report_published",
            report_id=row.id,
        )
        await rt.events.emit(investigation_id, "investigation_finished", status=status)
        return {}
