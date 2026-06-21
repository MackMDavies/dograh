"""Dograh MiniMax LLM wrapper.

MiniMax requires at least one user-role message in every chat completions
request. When the pipeline calls the LLM to generate the agent's opening
greeting the context contains only the system instruction — no user turn yet.
Sending that empty context results in:

    400 invalid params, chat content is empty (2013)

This wrapper detects the empty-user case and injects a one-shot synthetic
user trigger (``"..."``). The trigger is removed from the context immediately
after the call so it never appears in the conversation history visible to
subsequent turns.
"""

from __future__ import annotations

from loguru import logger

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.minimax.llm import MiniMaxLLMService

_SYNTHETIC_TRIGGER = {"role": "user", "content": "..."}


class DograhMiniMaxLLMService(MiniMaxLLMService):
    """MiniMaxLLMService with an empty-context guard for the opening turn."""

    async def _process_context(self, context: LLMContext) -> None:
        messages = context.messages or []
        has_user = any(m.get("role") == "user" for m in messages)

        if not has_user:
            logger.debug("DograhMiniMax: injecting synthetic user trigger (context has no user messages)")
            context.add_message(_SYNTHETIC_TRIGGER)
            try:
                await super()._process_context(context)
            finally:
                # Remove the synthetic trigger so it never becomes part of history
                if context.messages and context.messages[-1] == _SYNTHETIC_TRIGGER:
                    context.messages.pop()
        else:
            await super()._process_context(context)
