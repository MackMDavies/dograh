"""OpenAI LLM service tuned for live voice calls.

Pipecat's ``create_client`` builds the ``AsyncOpenAI`` client without passing
``max_retries`` or ``timeout``, and accepts ``**kwargs`` without forwarding
them — so the SDK defaults apply: two retries, with backoff driven by the
``Retry-After`` header OpenAI sends on a 429.

That is sensible for a batch job and wrong for a phone call. A rate-limited
account produced backoffs of 6.8s, 10.4s, 10.9s and 14.0s mid-conversation:
the pipeline blocked on the LLM, heartbeats stalled, and the caller heard dead
air until they hung up.

On a call a bounded failure beats an unbounded pause. Retries are disabled and
the request timeout capped, so a throttled provider surfaces an error the
pipeline can act on instead of freezing the conversation.
"""

import httpx
from loguru import logger
from openai import NOT_GIVEN
from openai import AsyncOpenAI
from openai import DefaultAsyncHttpxClient
from pipecat.services.openai.llm import OpenAILLMService

# Upper bound on a single LLM request. Beyond a few seconds of silence a caller
# assumes the line has dropped.
LLM_REQUEST_TIMEOUT_SECS = 12.0


class DograhOpenAILLMService(OpenAILLMService):
    """OpenAI service that fails fast rather than pausing a live call."""

    async def run_inference(
        self,
        context,
        max_tokens: int | None = None,
        system_instruction: str | None = None,
    ):
        """Out-of-band inference that doesn't send two conflicting token caps.

        Pipecat's ``build_chat_completion_params`` always writes BOTH
        ``max_tokens`` and ``max_completion_tokens`` into the request, relying
        on the NOT_GIVEN sentinel to drop whichever is unset. Its
        ``run_inference`` then decides which one to override with::

            if "max_completion_tokens" in params:

        — a key-presence test, and that key is unconditionally present, so the
        branch never falls through. It sets ``max_completion_tokens`` while
        leaving a configured ``max_tokens`` in place, and OpenAI rejects the
        pair outright:

            400 invalid_parameter_combination — Setting 'max_tokens' and
            'max_completion_tokens' at the same time is not supported.

        Every caller of run_inference is out-of-band work: context
        summarisation and variable extraction. So the failure is silent from
        the call's point of view — context compaction simply never happens,
        and the context grows unbounded on exactly the long calls that need it
        most. Observed on run 2871, a 119-second call, where both attempts
        failed.

        Setting one cap and clearing the other keeps the caller's intent and
        satisfies the API. Fixed here rather than in pipecat because ./api is
        bind-mounted on the box: this ships with a restart, where a pipecat
        change needs an image rebuild.
        """
        if max_tokens is not None:
            original = self._settings.max_tokens
            self._settings.max_tokens = NOT_GIVEN
            try:
                return await super().run_inference(
                    context,
                    max_tokens=max_tokens,
                    system_instruction=system_instruction,
                )
            finally:
                self._settings.max_tokens = original

        return await super().run_inference(
            context, max_tokens=max_tokens, system_instruction=system_instruction
        )

    def create_client(
        self,
        api_key=None,
        base_url=None,
        organization=None,
        project=None,
        default_headers=None,
        **kwargs,
    ):
        return AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            organization=organization,
            project=project,
            # No silent retries: the SDK would otherwise honour Retry-After and
            # sleep for as long as the provider asks, mid-conversation.
            max_retries=0,
            timeout=LLM_REQUEST_TIMEOUT_SECS,
            http_client=DefaultAsyncHttpxClient(
                limits=httpx.Limits(
                    max_keepalive_connections=100,
                    max_connections=1000,
                    keepalive_expiry=None,
                )
            ),
            default_headers=default_headers,
        )
