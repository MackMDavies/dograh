"""Dograh wrapper for AnthropicLLMService.

Anthropic's messages API requires:
  1. At least one message in the messages array.
  2. The first message must be role "user".

OpenAI allows calling with an empty messages list (just a system prompt) to
generate the very first bot response.  The pipecat AnthropicLLMAdapter does
not insert a synthetic user message to satisfy this constraint.  This module
provides a thin subclass that patches the invocation params before the API
call so bot-first agents work without modification to pipecat internals.
"""

from __future__ import annotations

from loguru import logger

from pipecat.services.anthropic.llm import AnthropicLLMService


class DograhAnthropicLLMService(AnthropicLLMService):
    """AnthropicLLMService with empty-messages guard and thinking disabled.

    Injects a synthetic "(call started)" user message when the messages array
    is empty or begins with an assistant turn, satisfying Anthropic's
    requirement that the first message must be from the user.

    Also explicitly disables extended thinking on every request.  The base
    class unconditionally adds betas=["interleaved-thinking-2025-05-14"]; when
    this beta is active, claude-opus-4-x generates large thinking blocks that
    delay TTS output by 7-10+ seconds.  Setting thinking.type="disabled"
    suppresses those blocks while keeping the beta header intact.
    """

    def _get_llm_invocation_params(self, context):
        params = super()._get_llm_invocation_params(context)
        messages = params.get("messages", [])
        if not messages or messages[0].get("role") != "user":
            params["messages"] = [{"role": "user", "content": "(call started)"}] + list(messages)
        return params

    async def _push_llm_text(self, text: str):
        logger.info(f"DograhAnthropicLLM: text→TTS ({len(text)}ch): {text[:80]!r}")
        await super()._push_llm_text(text)

    async def _create_message_stream(self, api_call, params):
        # Disable extended thinking explicitly.  The interleaved-thinking beta
        # is always added by the base class; without this guard, claude-opus-4-x
        # generates 5-15 second thinking blocks before producing any text,
        # which starves the TTS pipeline and results in total silence.
        if "thinking" not in params:
            params = {**params, "thinking": {"type": "disabled"}}
        return await super()._create_message_stream(api_call, params)
