"""The OpenAI client must not sit in a long silent retry during a live call.

Production incident: OpenAI returned 429 and the SDK's default retry policy
honoured the Retry-After header, sleeping 14 seconds mid-conversation. The
pipeline blocked waiting for the LLM, heartbeats stalled, the caller heard dead
air, said "Hello? Hello?" and hung up.

On a phone call a bounded failure beats an unbounded pause: surface the error
quickly so the pipeline can recover, rather than freezing the conversation.
"""

from api.services.pipecat.openai_llm import (
    LLM_REQUEST_TIMEOUT_SECS,
    DograhOpenAILLMService,
)


def _service():
    return DograhOpenAILLMService(api_key="sk-test-not-a-real-key", model="gpt-4o")


def test_client_does_not_retry_behind_the_callers_back():
    # The SDK default is 2 retries with Retry-After-driven backoff, which is
    # what produced the 14 second silence.
    assert _service()._client.max_retries == 0


def test_request_timeout_is_bounded():
    timeout = _service()._client.timeout
    seconds = getattr(timeout, "read", timeout)
    assert seconds is not None
    assert seconds <= LLM_REQUEST_TIMEOUT_SECS


def test_timeout_is_short_enough_to_be_survivable_on_a_call():
    # Anything beyond a few seconds of silence reads as a dropped call.
    assert LLM_REQUEST_TIMEOUT_SECS <= 15


def test_base_url_still_honoured_for_openai_compatible_providers():
    # xAI and other OpenAI-compatible providers go through this same class.
    svc = DograhOpenAILLMService(
        api_key="sk-test", model="grok-3", base_url="https://api.x.ai/v1"
    )
    assert "x.ai" in str(svc._client.base_url)
    assert svc._client.max_retries == 0
