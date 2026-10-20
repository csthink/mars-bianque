"""The evidence sub-agent: one implementation for the three evidence domains.

One task of the ``evidence_agent`` node runs the steps of one domain in one round, in
order. Each step is its own ReAct conversation: the model calls tools of its domain, the
executor runs them, the redacted results and their evidence IDs go back into the
conversation, and the model finally submits findings, which pass the evidence gate.

The node never raises. In LangGraph a failing ``Send`` task fails the whole superstep
and the results of the other sub-agents are lost, so every exception is caught here:
the unfinished steps are marked failed and the failure is reported in the state, from
where it reaches the missing evidence of the report.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from typing import Any

from bianque.agents.gated import SCHEMA_FAILURE, generate_gated, rejection_feedback
from bianque.agents.prompting import (
    EVIDENCE,
    FINDINGS,
    STEP,
    EvidenceView,
    FindingsView,
    build_prompt,
    prompt_text,
    render_evidence,
    render_findings,
    render_step,
    section,
)
from bianque.agents.snapshot import gate_context, load_snapshot
from bianque.agents.state import AgentTask, Failure, Runtime
from bianque.gate import Findings, GateResult, OutputType, validate
from bianque.llm import (
    CallContext,
    LLMError,
    RedactedMessage,
    RedactedPrompt,
    ToolCall,
    ToolSpec,
    assistant_message,
    redact_for_llm,
)
from bianque.llm.gateway import function_parameters
from bianque.storage.repositories import EvidenceRepository, PlanRepository
from bianque.tools import CallerScope, InvestigationContext, ToolResult

#: The function a sub-agent calls to finish a step.
SUBMIT_FINDINGS = "submit_findings"
SUBMIT_DESCRIPTION = (
    "Submit the findings of this step: short factual notes about the evidence, each citing "
    "the evidence IDs it rests on. Call it alone, after the tool results are in."
)

#: The answer to a ``submit_findings`` call made together with other tool calls.
SUBMIT_TOO_EARLY = (
    "Not submitted: call submit_findings alone, after you have read the results of your "
    "other tool calls."
)

#: The operation name of every model call of a sub-agent.
OPERATION = "evidence_agent"


def _now() -> datetime:
    return datetime.now(UTC)


def failure_category(error: BaseException) -> str:
    """Return a run-independent description of a sub-agent failure."""
    if isinstance(error, LLMError):
        return "model_unavailable"
    return f"failed: {type(error).__name__}"


class EvidenceAgent:
    """Runs the steps of one evidence domain."""

    def __init__(self, runtime: Runtime) -> None:
        """Bind the sub-agent to the runtime of one investigation run."""
        self._rt = runtime
        self._submit = ToolSpec(SUBMIT_FINDINGS, SUBMIT_DESCRIPTION, function_parameters(Findings))

    async def run(self, state: AgentTask) -> dict[str, Any]:
        """Run the task's steps in order; report failures instead of raising."""
        task = state
        failures: list[Failure] = []
        remaining = list(task["step_ids"])
        try:
            while remaining:
                await self._step(task["investigation_id"], task["domain"], remaining[0], failures)
                remaining.pop(0)
        except Exception as exc:
            failures.append(Failure(domain=task["domain"], tool=None, error=failure_category(exc)))
            await self._fail_steps(task["investigation_id"], task["domain"], remaining)
        return {"failures": failures}

    async def _fail_steps(
        self, investigation_id: str, domain: str, step_ids: Sequence[int]
    ) -> None:
        rt = self._rt
        async with rt.sessions() as session, session.begin():
            for step_id in step_ids:
                await PlanRepository(session).update_step(
                    step_id, status="failed", findings=[], finished_at=_now()
                )
        for step_id in step_ids:
            await rt.events.emit(
                investigation_id, "step_finished", step_id=step_id, domain=domain, status="failed"
            )

    async def _step(
        self, investigation_id: str, domain: str, step_id: int, failures: list[Failure]
    ) -> None:
        rt = self._rt
        async with rt.sessions() as session:
            plans = PlanRepository(session)
            step = await plans.get_step(step_id)
            versions = {p.id: p.version for p in await plans.list_plans(investigation_id)}
            earlier = [
                FindingsView(versions[s.plan_id], s.seq, s.domain, s.goal, tuple(s.findings))
                for s in await plans.list_steps(investigation_id)
                if s.domain == domain and s.findings is not None and s.id != step_id
            ]
            evidence = await EvidenceRepository(session).list(investigation_id)
        domain_tools = {spec.name for spec in rt.tools.specs(domain=domain)}
        alert = [
            EvidenceView(e.id, e.source, e.tool, e.redacted_content, tuple(e.identifiers))
            for e in evidence
            if e.source == "alertmanager"
        ]
        # Only this domain's own evidence, which no other sub-agent writes, so the
        # preserved set is the same on every run.
        preserve = {
            *rt.alert_identifiers,
            *(d.name for d in rt.tools),
            *(i for e in evidence if e.tool in domain_tools for i in e.identifiers),
        }
        async with rt.sessions() as session, session.begin():
            await PlanRepository(session).update_step(step_id, status="running", started_at=_now())
        await rt.events.emit(
            investigation_id, "step_started", step_id=step_id, domain=domain, status="running"
        )
        prompt = build_prompt(
            prompt_text("evidence_agent", domain=domain),
            [
                section(EVIDENCE, render_evidence(alert)),
                section(STEP, render_step(step.goal, step.suggested_tools, step.expected_evidence)),
                section(FINDINGS, render_findings(earlier)),
            ],
            engine=rt.engine,
            preserve=preserve,
        )
        context = CallContext(agent=domain, operation=OPERATION, investigation_id=investigation_id)
        conversation, collected, errors, submitted = await self._react(
            prompt, context, domain, step_id, preserve
        )
        failures += errors
        findings = await self._findings(conversation, context, submitted, preserve)
        status = "failed" if errors and not collected else "done"
        async with rt.sessions() as session, session.begin():
            await PlanRepository(session).update_step(
                step_id, status=status, findings=findings, finished_at=_now()
            )
        await rt.events.emit(
            investigation_id, "step_finished", step_id=step_id, domain=domain, status=status
        )

    async def _react(
        self,
        prompt: RedactedPrompt,
        context: CallContext,
        domain: str,
        step_id: int,
        preserve: set[str],
    ) -> tuple[RedactedPrompt, list[str], list[Failure], tuple[ToolCall, str] | None]:
        """Run the tool loop of one step.

        Returns:
            The conversation, the evidence IDs collected, the failed calls, and the
            ``submit_findings`` call with the text of its turn, when the model made one.
        """
        rt = self._rt
        assert context.investigation_id is not None
        specs = [*rt.tools.specs(domain=domain), self._submit]
        conversation = prompt
        collected: list[str] = []
        errors: list[Failure] = []
        for _ in range(rt.subagent_max_steps):
            completion = await rt.gateway.complete(
                conversation, context=context, tools=specs, tool_choice="auto"
            )
            calls = completion.tool_calls
            tool_calls = [call for call in calls if call.name != SUBMIT_FINDINGS]
            if not tool_calls:
                submit = next((call for call in calls if call.name == SUBMIT_FINDINGS), None)
                if submit is not None:
                    return conversation, collected, errors, (submit, completion.text)
                conversation = conversation + (
                    assistant_message(completion.text, engine=rt.engine, preserve=preserve),
                )
                break
            conversation = conversation + (
                assistant_message(completion.text, calls, engine=rt.engine, preserve=preserve),
            )
            results = await asyncio.gather(
                *(
                    rt.executor.call(
                        call.name,
                        call.arguments,
                        scope=CallerScope(agent=domain, domain=domain),
                        investigation=InvestigationContext(context.investigation_id, step_id),
                        preserve=preserve,
                    )
                    for call in tool_calls
                )
            )
            by_call = {call.id: result for call, result in zip(tool_calls, results, strict=True)}
            for done in results:
                preserve.update(done.identifiers)
            # Results go back in the order the model gave the calls, whatever order the
            # calls finished in.
            messages = []
            for call in calls:
                outcome = by_call.get(call.id)
                messages.append(self._tool_message(call, outcome, preserve))
                if outcome is None:
                    continue
                if outcome.status == "ok":
                    collected += outcome.evidence_ids
                    await self._announce(context.investigation_id, call.name, outcome)
                elif outcome.status == "error":
                    category = (outcome.error or "failed").split(":", 1)[0]
                    errors.append(Failure(domain=domain, tool=call.name, error=category))
            conversation = conversation + tuple(messages)
        return conversation, collected, errors, None

    def _tool_message(
        self, call: ToolCall, result: ToolResult | None, preserve: Collection[str]
    ) -> RedactedMessage:
        if result is None:
            text = SUBMIT_TOO_EARLY
        elif result.status == "ok":
            text = f"Evidence ID: {', '.join(result.evidence_ids)}\n{result.model_view.text}"
        else:
            text = f"Error: {result.model_view.text}"
        return RedactedMessage(
            "tool",
            redact_for_llm(text, engine=self._rt.engine, preserve=preserve),
            tool_call_id=call.id,
        )

    async def _announce(self, investigation_id: str, tool: str, result: ToolResult) -> None:
        rt = self._rt
        definition = rt.tools.get(tool)
        for evidence_id in result.evidence_ids:
            if evidence_id in rt.announced:
                continue
            rt.announced.add(evidence_id)
            await rt.events.emit(
                investigation_id,
                "evidence_added",
                evidence_id=evidence_id,
                source=definition.source if definition else None,
                tool=tool,
            )

    async def _findings(
        self,
        conversation: RedactedPrompt,
        context: CallContext,
        submitted: tuple[ToolCall, str] | None,
        preserve: Collection[str],
    ) -> list[dict[str, Any]]:
        """Return the findings that pass the gate, or none (the evidence stays).

        A ``submit_findings`` call the model made on its own is validated first; after a
        rejection, or when the model made none, the call is forced, with one retry.
        """
        rt = self._rt
        assert context.investigation_id is not None
        snapshot = await load_snapshot(rt.sessions, context.investigation_id)
        gate = gate_context(snapshot, rt)
        retries = 1
        if submitted is not None:
            call, text = submitted
            try:
                arguments: Any = json.loads(call.arguments)
            except json.JSONDecodeError:
                result = GateResult(OutputType.FINDINGS.value, (SCHEMA_FAILURE,))
            else:
                result = validate(OutputType.FINDINGS, arguments, gate)
            await rt.recorder.set_gate_result(
                context.investigation_id, context.agent, context.operation, result.to_json()
            )
            if result.passed and isinstance(result.output, Findings):
                return [finding.model_dump(mode="json") for finding in result.output.findings]
            conversation = conversation + rejection_feedback(
                call, text, result.violations, engine=rt.engine, preserve=preserve
            )
            retries = 0
        gated = await generate_gated(
            rt.gateway,
            rt.recorder,
            conversation,
            context=context,
            output_type=OutputType.FINDINGS,
            function_name=SUBMIT_FINDINGS,
            description=SUBMIT_DESCRIPTION,
            gate_context=gate,
            preserve=preserve,
            retries=retries,
        )
        if isinstance(gated.output, Findings):
            return [finding.model_dump(mode="json") for finding in gated.output.findings]
        return []
