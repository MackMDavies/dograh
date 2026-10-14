"""Supervisor Whisper / Take over (services/supervisor/live_control.py).

The pipeline lives on one uvicorn worker and the supervisor's request lands on any
other, so the pieces are tested at the seams that matter: what a portal request is
allowed to become, what a whisper turns into inside the LLM context, and that a
take-over the supervisor does NOT answer leaves the AI on the call.
"""

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services.supervisor import live_control as lc
from api.services.supervisor.live_control import (
    SupervisorCommandError,
    SupervisorControl,
    validate_command,
    whisper_message,
)


def test_whisper_command_is_trimmed_and_bounded():
    assert validate_command({"type": "whisper", "text": "  offer the 10% plan  "}) == {
        "type": "whisper",
        "text": "offer the 10% plan",
    }
    with pytest.raises(SupervisorCommandError):
        validate_command({"type": "whisper", "text": "   "})
    with pytest.raises(SupervisorCommandError):
        validate_command({"type": "whisper", "text": "x" * (lc.MAX_WHISPER_CHARS + 1)})


def test_takeover_needs_an_e164_number_and_strips_formatting():
    assert validate_command(
        {"type": "takeover", "destination": "+1 (415) 555-0123"}
    ) == {
        "type": "takeover",
        "destination": "+14155550123",
    }
    for bad in ["4155550123", "+0123456789", "", "PJSIP/100", "+1415555012345678"]:
        with pytest.raises(SupervisorCommandError):
            validate_command({"type": "takeover", "destination": bad})


def test_unknown_commands_are_refused():
    with pytest.raises(SupervisorCommandError):
        validate_command({"type": "hangup"})


def test_whisper_is_framed_as_private_and_keeps_the_text():
    msg = whisper_message("Ask for the postcode")
    assert msg["role"] == "user"
    assert msg["content"].endswith("Ask for the postcode")
    assert "cannot hear" in msg["content"]


@pytest.mark.asyncio
async def test_whisper_appends_to_context_without_forcing_a_reply():
    captured = {}

    class FakeAppend:
        def __init__(self, messages, run_llm=True):
            captured["messages"], captured["run_llm"] = messages, run_llm

    frames = types.ModuleType("pipecat.frames.frames")
    frames.LLMMessagesAppendFrame = FakeAppend
    engine = MagicMock()
    engine.task.queue_frame = AsyncMock()
    with patch.dict(sys.modules, {"pipecat.frames.frames": frames}):
        await SupervisorControl(engine, 7, 1).whisper("Mention the free trial")
    engine.task.queue_frame.assert_awaited_once()
    assert captured["run_llm"] is False
    assert captured["messages"][0]["content"].endswith("Mention the free trial")


@pytest.mark.asyncio
async def test_bad_commands_from_the_channel_are_dropped_not_raised():
    control = SupervisorControl(MagicMock(), 7, 1)
    control.whisper = AsyncMock()
    control.take_over = AsyncMock()
    await control._handle({"type": "whisper", "text": ""})
    await control._handle({"type": "nope"})
    control.whisper.assert_not_awaited()
    control.take_over.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_second_takeover_while_one_is_ringing_is_ignored():
    control = SupervisorControl(MagicMock(), 7, 1)
    control._take_over = AsyncMock()
    control._takeover_running = True
    await control.take_over("+14155550123")
    control._take_over.assert_not_awaited()


def _takeover_modules(event, provider):
    """Fake the modules _take_over imports lazily (pipecat + db + telephony)."""
    enums = types.ModuleType("pipecat.utils.enums")
    enums.EndTaskReason = types.SimpleNamespace(
        TRANSFER_CALL=types.SimpleNamespace(value="transfer_call")
    )
    run = types.SimpleNamespace(mode="twilio", gathered_context={"call_id": "CA123"})
    db = types.ModuleType("api.db")
    db.db_client = types.SimpleNamespace(
        get_workflow_run_by_id=AsyncMock(return_value=run)
    )
    api_enums = types.ModuleType("api.enums")
    api_enums.WorkflowRunMode = types.SimpleNamespace(
        WEBRTC=types.SimpleNamespace(value="webrtc"),
        SMALLWEBRTC=types.SimpleNamespace(value="smallwebrtc"),
        TEXTCHAT=types.SimpleNamespace(value="textchat"),
    )
    playback = types.ModuleType("api.services.pipecat.audio_playback")
    playback.play_audio_loop = AsyncMock()
    manager = MagicMock()
    manager.store_transfer_context = AsyncMock()
    manager.remove_transfer_context = AsyncMock()
    manager.wait_for_transfer_completion = AsyncMock(return_value=event)
    ctm = types.ModuleType("api.services.telephony.call_transfer_manager")
    ctm.get_call_transfer_manager = AsyncMock(return_value=manager)
    factory = types.ModuleType("api.services.telephony.factory")
    factory.get_telephony_provider_for_run = AsyncMock(return_value=provider)
    proto = types.ModuleType("api.services.telephony.transfer_event_protocol")
    proto.TransferContext = lambda **kw: types.SimpleNamespace(**kw)
    return manager, {
        "pipecat.utils.enums": enums,
        "api.db": db,
        "api.enums": api_enums,
        "api.services.pipecat.audio_playback": playback,
        "api.services.telephony.call_transfer_manager": ctm,
        "api.services.telephony.factory": factory,
        "api.services.telephony.transfer_event_protocol": proto,
    }


def _provider():
    p = MagicMock()
    p.supports_transfers.return_value = True
    p.validate_config.return_value = True
    p.transfer_call = AsyncMock(return_value={"call_sid": "CA999"})
    return p


@pytest.mark.asyncio
async def test_answered_takeover_ends_the_ai_with_transfer_reason():
    answered = MagicMock()
    answered.to_result_dict.return_value = {"action": "destination_answered"}
    provider = _provider()
    manager, mods = _takeover_modules(answered, provider)
    engine = MagicMock()
    engine._transport_output = None
    engine.end_call_with_reason = AsyncMock()
    with patch.dict(sys.modules, mods):
        await SupervisorControl(engine, 7, 1).take_over("+14155550123")
    provider.transfer_call.assert_awaited_once()
    assert (
        provider.transfer_call.await_args.kwargs["conference_name"] == "transfer-CA123"
    )
    engine.end_call_with_reason.assert_awaited_once_with(
        "transfer_call", abort_immediately=False
    )
    assert engine.set_mute_pipeline.call_args_list[-1].args == (False,)


@pytest.mark.asyncio
async def test_unanswered_takeover_leaves_the_ai_on_the_call():
    provider = _provider()
    manager, mods = _takeover_modules(None, provider)
    engine = MagicMock()
    engine._transport_output = None
    engine.end_call_with_reason = AsyncMock()
    with patch.dict(sys.modules, mods):
        await SupervisorControl(engine, 7, 1).take_over("+14155550123")
    engine.end_call_with_reason.assert_not_awaited()
    manager.remove_transfer_context.assert_awaited_once()
    assert engine.set_mute_pipeline.call_args_list[-1].args == (False,)


@pytest.mark.asyncio
async def test_takeover_is_skipped_when_the_provider_cannot_transfer():
    provider = _provider()
    provider.supports_transfers.return_value = False
    manager, mods = _takeover_modules(None, provider)
    engine = MagicMock()
    with patch.dict(sys.modules, mods):
        await SupervisorControl(engine, 7, 1).take_over("+14155550123")
    provider.transfer_call.assert_not_awaited()
    engine.set_mute_pipeline.assert_not_called()
