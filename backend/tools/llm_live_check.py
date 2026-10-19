r"""Check a real model through the gateway: plain text, tool calling and structured output.

Runs three calls in ``live`` mode with the current ``BIANQUE_LLM_*`` settings and prints
one JSON line per call: operation, model, latency, token usage, cost, and what came
back (a short preview of text, the names of called tools, or whether the structured
output validated). It never prints the credential or the prompts.

    BIANQUE_LLM_MODE=live uv run python -m tools.llm_live_check \\
        --input-price 0.3 --output-price 1.2

Prices are US dollars per million tokens, taken from the provider's price page on the
day of the check; they are arguments because they change.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Literal

from pydantic import BaseModel, Field

from bianque.config import get_settings
from bianque.llm import (
    CallContext,
    MemoryCallRecorder,
    StructuredOutputError,
    ToolSpec,
    build_gateway,
    build_redaction_engine,
    redact_prompt_for_llm,
)

SYSTEM = "You are a Kubernetes troubleshooting assistant. Answer briefly."
EVENTS = (
    "Pod demo-s1/s1-app-7d9f8c6b5-x2k4p: container app terminated, reason OOMKilled, "
    "exit code 137, restart count 4; memory limit 64Mi."
)

GET_POD = ToolSpec(
    name="get_pod",
    description="Read the status of one pod.",
    parameters={
        "type": "object",
        "properties": {
            "namespace": {"type": "string"},
            "name": {"type": "string"},
        },
        "required": ["namespace", "name"],
    },
)


class Diagnosis(BaseModel):
    """The structured output requested in the third call."""

    category: Literal["resource_limit", "config_error", "probe_misconfig", "image_error", "unknown"]
    summary: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


def _cost(
    prompt_tokens: int, completion_tokens: int, input_price: float, output_price: float
) -> float:
    return (prompt_tokens * input_price + completion_tokens * output_price) / 1_000_000


async def _run(input_price: float, output_price: float) -> int:
    settings = get_settings()
    if settings.llm_mode != "live":
        print("set BIANQUE_LLM_MODE=live", file=sys.stderr)
        return 2
    recorder = MemoryCallRecorder()
    gateway = build_gateway(settings, recorder=recorder)
    engine = build_redaction_engine(settings)

    text = await gateway.complete(
        redact_prompt_for_llm(
            [("system", SYSTEM), ("user", f"{EVENTS}\nIn one sentence, what happened?")],
            engine=engine,
        ),
        context=CallContext(agent="investigation", operation="check_text"),
    )
    tools = await gateway.complete(
        redact_prompt_for_llm(
            [("system", SYSTEM), ("user", f"{EVENTS}\nRead the pod's status before answering.")],
            engine=engine,
        ),
        context=CallContext(agent="kubernetes", operation="check_tools"),
        tools=(GET_POD,),
    )
    outcomes: dict[str, dict[str, object]] = {
        "check_text": {"text_preview": text.text[:80]},
        "check_tools": {"tool_calls": [call.name for call in tools.tool_calls]},
    }
    try:
        structured = await gateway.complete_structured(
            redact_prompt_for_llm(
                [("system", SYSTEM), ("user", f"{EVENTS}\nDiagnose the root cause.")],
                engine=engine,
            ),
            context=CallContext(agent="investigation", operation="check_structured"),
            output=Diagnosis,
            function_name="submit_diagnosis",
            description="Submit the diagnosis.",
        )
    except StructuredOutputError as exc:
        outcomes["check_structured"] = {"valid": False, "error": str(exc)}
    else:
        outcomes["check_structured"] = {
            "valid": True,
            "attempts": structured.attempts,
            "category": structured.value.category,
        }
    for event in recorder.events:
        tokens_in = event.tokens_in or 0
        tokens_out = event.tokens_out or 0
        line = {
            "operation": event.name,
            "status": event.status,
            "model": settings.llm_model,
            "latency_ms": event.latency_ms,
            "retries": event.retries,
            "prompt_tokens": tokens_in,
            "completion_tokens": tokens_out,
            "cost_usd": round(_cost(tokens_in, tokens_out, input_price, output_price), 6),
            "output_preview": (event.output_summary or "")[:160],
            **outcomes.get(event.name, {}),
        }
        print(json.dumps(line, ensure_ascii=False))
    return 0 if outcomes["check_structured"]["valid"] else 1


def main(argv: list[str] | None = None) -> int:
    """Parse prices and run the three calls."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input-price", type=float, required=True, help="USD per 1M input tokens")
    parser.add_argument(
        "--output-price", type=float, required=True, help="USD per 1M output tokens"
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.input_price, args.output_price))


if __name__ == "__main__":
    raise SystemExit(main())
