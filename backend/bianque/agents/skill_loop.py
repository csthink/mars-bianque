"""The short loop before a plan is submitted: the model may load skills first.

A planning request offers two functions, ``activate_skill`` and ``submit_plan``, and the
model must call one of them. An ``activate_skill`` call is answered with the skill's
content (or a note that it is already loaded) as the function result, and the model is
asked again. The loop ends when the model calls ``submit_plan``, when it calls nothing,
or when the activation limit is used up; then the caller validates the submitted plan,
or forces ``submit_plan`` with only that function offered.

Every call other than ``submit_plan`` counts toward the limit, including a call that
names no skill of the catalog and a call the limit refuses, so the loop always ends.
When the model cannot be called, the loop ends and says so; the caller then falls back
to the default plan as it does when a forced plan call finds the model unavailable.
Each activation of a skill, first or repeated, is stored as a call record of kind
``skill``; the skill's content is never stored as evidence.
"""

from __future__ import annotations

import time
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from bianque.agents.state import Runtime
from bianque.llm import (
    CallContext,
    LLMUnavailableError,
    RedactedMessage,
    RedactedPrompt,
    ToolCall,
    ToolSpec,
    assistant_message,
    redact_for_llm,
    tool_result_message,
)
from bianque.skills import (
    ACTIVATE_DESCRIPTION,
    ACTIVATE_SKILL,
    Activation,
    activate,
    activation_parameters,
)
from bianque.storage.repositories import CallRecordRepository

#: The answer to an ``activate_skill`` call beyond the limit.
LIMIT_REACHED = "Not loaded: the skill activation limit is reached. Call {submit} now."

#: The answer to a call of a function that was not offered.
NOT_OFFERED = "Not available: call {activate} or {submit}."


@dataclass(frozen=True)
class SkillLoopOutcome:
    """Where the loop left the planning conversation.

    Attributes:
        conversation: The prompt with every activation exchange appended. A submitted
            call's own turn is not appended; the caller adds it with any feedback.
        loaded: The skills loaded in the investigation, in load order, after the loop.
        submitted: The ``submit_plan`` call and the text of its turn, when the model made
            one.
        unavailable: The model could not be called; retries were used up.
    """

    conversation: RedactedPrompt
    loaded: tuple[str, ...]
    submitted: tuple[ToolCall, str] | None
    unavailable: bool = False


class SkillLoop:
    """Runs the activation loop of one plan generation."""

    def __init__(self, runtime: Runtime, submit: ToolSpec) -> None:
        """Bind the loop to the runtime and the plan's submit function."""
        self._rt = runtime
        self._submit = submit
        self._activate = ToolSpec(
            ACTIVATE_SKILL, ACTIVATE_DESCRIPTION, activation_parameters(runtime.skills)
        )

    async def run(
        self,
        prompt: RedactedPrompt,
        context: CallContext,
        loaded: Sequence[str],
        preserve: Collection[str],
    ) -> SkillLoopOutcome:
        """Let the model load skills until it submits, stops, or reaches the limit.

        Args:
            prompt: The planning prompt.
            context: The plan or replan call site of the investigation.
            loaded: The skills loaded earlier in the investigation.
            preserve: Identifiers the redaction of the conversation keeps.
        """
        rt = self._rt
        conversation = prompt
        names = list(loaded)
        calls_made = 0
        while calls_made < rt.max_skill_activations:
            try:
                completion = await rt.gateway.complete(
                    conversation,
                    context=context,
                    tools=(self._activate, self._submit),
                    tool_choice="required",
                )
            except LLMUnavailableError:
                return SkillLoopOutcome(conversation, tuple(names), None, unavailable=True)
            calls = completion.tool_calls
            submit = next((call for call in calls if call.name == self._submit.name), None)
            if submit is not None:
                return SkillLoopOutcome(conversation, tuple(names), (submit, completion.text))
            if not calls:
                conversation = conversation + (
                    assistant_message(completion.text, engine=rt.engine, preserve=preserve),
                )
                break
            conversation = conversation + (
                assistant_message(completion.text, calls, engine=rt.engine, preserve=preserve),
            )
            results: list[RedactedMessage] = []
            for call in calls:
                calls_made += 1
                text = await self._answer(call, calls_made, names, context)
                results.append(
                    tool_result_message(call.id, text, engine=rt.engine, preserve=preserve)
                )
            conversation = conversation + tuple(results)
        return SkillLoopOutcome(conversation, tuple(names), None)

    async def _answer(
        self, call: ToolCall, number: int, names: list[str], context: CallContext
    ) -> str:
        """Answer one call; record and remember an activation of a skill."""
        if call.name != ACTIVATE_SKILL:
            return NOT_OFFERED.format(activate=ACTIVATE_SKILL, submit=self._submit.name)
        if number > self._rt.max_skill_activations:
            return LIMIT_REACHED.format(submit=self._submit.name)
        started_at, clock = datetime.now(UTC), time.monotonic()
        activation = activate(self._rt.skills, names, call.arguments)
        if activation.name is not None:
            if activation.first:
                names.append(activation.name)
            await self._record(activation, context, started_at, clock)
        return activation.result

    async def _record(
        self, activation: Activation, context: CallContext, started_at: datetime, clock: float
    ) -> None:
        rt = self._rt
        assert activation.name is not None
        keep = rt.skills.names
        async with rt.sessions() as session, session.begin():
            await CallRecordRepository(session).add(
                agent=context.agent,
                kind="skill",
                name=activation.name,
                input_summary=redact_for_llm(activation.name, engine=rt.engine, preserve=keep).text,
                output_summary=redact_for_llm(
                    activation.summary, engine=rt.engine, preserve=keep
                ).text,
                status="ok",
                started_at=started_at,
                latency_ms=round((time.monotonic() - clock) * 1000),
                investigation_id=context.investigation_id,
            )
