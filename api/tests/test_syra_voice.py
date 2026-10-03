import pytest

from api.services.syra_voice import is_syra_voice_config, is_syra_voice_workflow, syra_speech_speed


def test_only_an_explicit_true_exempts_a_workflow():
    assert is_syra_voice_config({"syra_voice": True}) is True
    for cfg in (None, {}, {"syra_voice": "true"}, {"syra_voice": 1}, {"syra_voice": False}, {"other": True}):
        assert is_syra_voice_config(cfg) is False


def test_syra_speech_speed_is_bounded_and_rejects_non_numeric_values():
    assert syra_speech_speed(0.85) == 0.85
    assert syra_speech_speed(1.02) == 1.02
    assert syra_speech_speed(1.2) == 1.2
    assert syra_speech_speed(0.1) == 0.85
    assert syra_speech_speed(4.0) == 1.2
    for value in (None, True, "1.1", float("nan"), float("inf")):
        assert syra_speech_speed(value) is None


@pytest.mark.asyncio
async def test_a_missing_workflow_is_never_exempt():
    assert await is_syra_voice_workflow(None) is False
