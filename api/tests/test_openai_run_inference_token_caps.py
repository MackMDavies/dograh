"""Out-of-band inference must not send two conflicting token caps.

pipecat's build_chat_completion_params always writes BOTH `max_tokens` and
`max_completion_tokens` into the request and relies on the NOT_GIVEN sentinel
to drop whichever is unset. Its run_inference then picks which to override
with `if "max_completion_tokens" in params:` — a key-presence test against a
key that is unconditionally present, so it always sets max_completion_tokens
and never clears a configured max_tokens. OpenAI rejects the pair:

    400 invalid_parameter_combination — Setting 'max_tokens' and
    'max_completion_tokens' at the same time is not supported.

Run 2871, a 119-second call, hit this on both compaction attempts. The failure
is invisible on the call — context compaction silently never happens, on
exactly the long calls that need it.
"""

from unittest.mock import AsyncMock, patch

import pytest
from openai import NOT_GIVEN

from api.services.pipecat.openai_llm import DograhOpenAILLMService


def _service(**settings):
    svc = DograhOpenAILLMService(api_key="test-key", model="gpt-4.1-mini")
    for k, v in settings.items():
        setattr(svc._settings, k, v)
    return svc


async def _captured_params(svc, **kwargs) -> dict:
    """Run inference against a stubbed client and return the request params."""
    captured = {}

    async def _create(**params):
        captured.update(params)

        class _Msg:
            content = "ok"

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()

    with patch.object(type(svc), "get_llm_adapter") as adapter:
        adapter.return_value.get_llm_invocation_params.return_value = {"messages": []}
        svc._client.chat.completions.create = _create
        await svc.run_inference(object(), **kwargs)
    return captured


class TestTokenCaps:
    @pytest.mark.asyncio
    async def test_a_configured_max_tokens_is_cleared_when_overriding(self):
        # The exact shape that 400'd: agent config carries max_tokens, and the
        # summariser passes its own budget.
        svc = _service(max_tokens=4096, max_completion_tokens=NOT_GIVEN)
        params = await _captured_params(svc, max_tokens=250)

        assert params.get("max_completion_tokens") == 250
        assert params.get("max_tokens", NOT_GIVEN) is NOT_GIVEN, (
            "max_tokens must not be sent alongside max_completion_tokens"
        )

    @pytest.mark.asyncio
    async def test_the_service_setting_survives_the_call(self):
        # Streaming completions on the live call still need it afterwards.
        svc = _service(max_tokens=4096, max_completion_tokens=NOT_GIVEN)
        await _captured_params(svc, max_tokens=250)
        assert svc._settings.max_tokens == 4096

    @pytest.mark.asyncio
    async def test_nothing_is_touched_when_no_override_is_requested(self):
        svc = _service(max_tokens=4096, max_completion_tokens=NOT_GIVEN)
        params = await _captured_params(svc)
        assert params.get("max_tokens") == 4096
        assert svc._settings.max_tokens == 4096
