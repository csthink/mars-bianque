"""Provider for ``scripted`` mode: answers come from ``bianque.llm.scripted`` rules."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from bianque.llm.request import ModelRequest
from bianque.llm.scripted import ScriptedResponder, ScriptedRule, to_tool_calls
from bianque.llm.types import LLMCompletion, LLMUsage, estimate_tokens

logger = logging.getLogger(__name__)

#: Model name reported in ``scripted`` mode.
SCRIPTED_MODEL = "scripted"


class ScriptedProvider:
    """Provider with no network access whose answers come from rules."""

    def __init__(self, rules: Sequence[ScriptedRule] = ()) -> None:
        """Build the responder and warn that no real model is in use.

        Args:
            rules: Rules tried before the built-in fallback rule.
        """
        self._responder = ScriptedResponder(tuple(rules))
        logger.warning("model gateway is in scripted mode: answers come from rules, not a model")

    @property
    def name(self) -> str:
        """The name of this implementation."""
        return "scripted"

    @property
    def endpoint_host(self) -> str | None:
        """Always ``None``: scripted mode has no network endpoint."""
        return None

    async def complete(self, request: ModelRequest) -> LLMCompletion:
        """Answer with the first matching rule; usage is estimated from text length."""
        reply = self._responder.reply(request)
        tool_calls = to_tool_calls(reply)
        prompt_text = "".join(message.content.text for message in request.prompt.messages)
        output_text = reply.text + "".join(call.arguments for call in tool_calls)
        return LLMCompletion(
            text=reply.text,
            model=SCRIPTED_MODEL,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
            usage=LLMUsage(
                prompt_tokens=estimate_tokens(prompt_text),
                completion_tokens=estimate_tokens(output_text),
                estimated=True,
            ),
        )

    def __repr__(self) -> str:
        """Show the implementation name."""
        return "ScriptedProvider()"
