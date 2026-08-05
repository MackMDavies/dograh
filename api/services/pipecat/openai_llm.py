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
from openai import AsyncOpenAI
from openai import DefaultAsyncHttpxClient
from pipecat.services.openai.llm import OpenAILLMService

# Upper bound on a single LLM request. Beyond a few seconds of silence a caller
# assumes the line has dropped.
LLM_REQUEST_TIMEOUT_SECS = 12.0


class DograhOpenAILLMService(OpenAILLMService):
    """OpenAI service that fails fast rather than pausing a live call."""

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
