import pytest

from api.services.syra_voice import is_syra_voice_config, is_syra_voice_workflow


def test_only_an_explicit_true_exempts_a_workflow():
    assert is_syra_voice_config({"syra_voice": True}) is True
    for cfg in (None, {}, {"syra_voice": "true"}, {"syra_voice": 1}, {"syra_voice": False}, {"other": True}):
        assert is_syra_voice_config(cfg) is False


@pytest.mark.asyncio
async def test_a_missing_workflow_is_never_exempt():
    assert await is_syra_voice_workflow(None) is False
