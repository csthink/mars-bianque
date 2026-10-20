"""Generate a structured model output and pass it through the evidence gate.

Plans, assessments and findings share one flow: get the output
by a forced function call, validate it, and when the gate rejects it, show the model its
call and the violations and ask once more. When the retry is rejected too, the caller
falls back (default plan, deterministic rule, no findings); a rejected output is never
stored anywhere but in the model call record.

The violations shown to the model name the validator, the JSON path and a fixed code,
never text of the output, so the feedback holds nothing the gate rejected.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

from pydantic import BaseModel

from bianque.agents.prompting import render_violations
from bianque.agents.records import DatabaseCallRecorder
from bianque.gate import SCHEMAS, GateContext, GateResult, OutputType, Violation, validate
from bianque.llm import (
    CallContext,
    LLMGateway,
    LLMUnavailableError,
    RedactedMessage,
    RedactedPrompt,
    StructuredOutputError,
    ToolCall,
    assistant_message,
    redact_for_llm,
    tool_result_message,
)
from bianque.redaction import RedactionEngine

#: The violation recorded when the model gave no output that matches the schema.
SCHEMA_FAILURE = Violation("schema", "(root)", "invalid_arguments")

#: The violation recorded when the model could not be called.
MODEL_UNAVAILABLE = Violation("model", "(root)", "model_unavailable")


@dataclass(frozen=True)
class GatedOutput:
    """The outcome of a gated generation.

    Attributes:
        output: The output that passed the gate, or ``None`` when every attempt failed.
        results: The gate result of each attempt.
    """

    output: BaseModel | None
    results: tuple[GateResult, ...] = field(default_factory=tuple)

    @property
    def last_violations(self) -> tuple[Violation, ...]:
        """The violations of the last attempt."""
        return self.results[-1].violations if self.results else ()


def rejection_feedback(
    call: ToolCall,
    text: str,
    violations: Collection[Violation],
    *,
    engine: RedactionEngine,
    preserve: Collection[str],
) -> tuple[RedactedMessage, ...]:
    """Return the messages that show the model its rejected call and why it was rejected."""
    return (
        assistant_message(text, (call,), engine=engine, preserve=preserve),
        tool_result_message(
            call.id,
            "The evidence gate rejected these arguments:\n"
            f"{render_violations(violations)}\n"
            f"Call {call.name} again with arguments that pass these checks.",
            engine=engine,
            preserve=preserve,
        ),
    )


async def generate_gated(
    gateway: LLMGateway,
    recorder: DatabaseCallRecorder,
    prompt: RedactedPrompt,
    *,
    context: CallContext,
    output_type: OutputType,
    function_name: str,
    description: str,
    gate_context: GateContext,
    preserve: Collection[str],
    retries: int = 1,
) -> GatedOutput:
    """Generate an output of ``output_type`` and validate it, retrying after rejections.

    Args:
        gateway: The model gateway.
        recorder: Receives the gate result of each call.
        prompt: The redacted prompt.
        context: Who makes the call; ``investigation_id`` must be set.
        output_type: The output type and with it the schema and the checklist.
        function_name: The function the model is forced to call.
        description: The function's description.
        gate_context: What the output is checked against.
        preserve: Identifiers the redaction of the feedback keeps.
        retries: Further attempts after a rejection.

    Returns:
        The passed output, or ``None`` with the gate results of the failed attempts.
    """
    assert context.investigation_id is not None
    engine = gate_context.engine
    conversation = prompt
    results: list[GateResult] = []
    for _ in range(retries + 1):
        try:
            structured = await gateway.complete_structured(
                conversation,
                context=context,
                output=SCHEMAS[output_type],
                function_name=function_name,
                description=description,
            )
        except StructuredOutputError:
            result = GateResult(output_type.value, (SCHEMA_FAILURE,))
            results.append(result)
            await recorder.set_gate_result(
                context.investigation_id, context.agent, context.operation, result.to_json()
            )
            conversation = conversation + (
                RedactedMessage(
                    "user",
                    redact_for_llm(
                        f"Your call of {function_name} did not match its schema. "
                        f"Call {function_name} with arguments that satisfy it.",
                        engine=engine,
                    ),
                ),
            )
            continue
        except LLMUnavailableError:
            # Only an unavailable model falls back; any other model error (a missing
            # recording, a rejected request) ends the investigation.
            results.append(GateResult(output_type.value, (MODEL_UNAVAILABLE,)))
            break
        result = validate(output_type, structured.value, gate_context)
        results.append(result)
        await recorder.set_gate_result(
            context.investigation_id, context.agent, context.operation, result.to_json()
        )
        if result.passed:
            return GatedOutput(result.output, tuple(results))
        call = next(c for c in structured.completion.tool_calls if c.name == function_name)
        conversation = conversation + rejection_feedback(
            call, structured.completion.text, result.violations, engine=engine, preserve=preserve
        )
    return GatedOutput(None, tuple(results))
