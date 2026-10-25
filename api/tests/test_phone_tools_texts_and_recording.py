"""Texting the caller and pausing the recording (Keypad & Native Phone Tools, phase 3)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from api.services.phone_tools import builtin
from api.services.phone_tools.policy import normalize_policy
from api.services.workflow.pipecat_engine import PipecatEngine
from api.services.workflow.pipecat_engine_context_composer import compose_functions_for_node

_NODE = SimpleNamespace(document_uuids=[], tool_uuids=[], out_edges=[])
LIVE_URL = "https://ref.supabase.co/functions/v1/dograh-call-live"


def _engine(policy=None, **kw):
    engine = PipecatEngine(llm=Mock(register_function=Mock()), workflow=Mock(), call_context_vars={}, workflow_run_id=5)
    engine.set_phone_tools(policy or normalize_policy(None), logs_buffer=Mock(append=AsyncMock()), **kw)
    return engine


def _registered(engine, name):
    return next(c.args[1] for c in engine.llm.register_function.call_args_list if c.args[0] == name)


# --- rules -----------------------------------------------------------------------


def test_new_tools_are_on_by_default_and_start_paused_is_recording_only():
    policy = normalize_policy(None)
    assert policy.enabled("sms") and policy.enabled("recording")
    assert not policy.recording_starts_paused()
    saved = normalize_policy({"tools": {
        "recording": {"mode": "on", "start_paused": True},
        "sms": {"mode": "on", "start_paused": True},
    }}).to_dict()
    assert saved["tools"]["recording"]["start_paused"] is True
    assert "start_paused" not in saved["tools"]["sms"]
    assert not normalize_policy({"tools": {"recording": {"mode": "off", "start_paused": True}}}).recording_starts_paused()


# --- which tools a call offers ---------------------------------------------------


def test_texting_needs_a_number_the_hook_and_a_real_call(monkeypatch):
    monkeypatch.setenv("SYSEVO_CALL_LIVE_URL", LIVE_URL)
    assert builtin.SEND_TEXT in _engine(caller_number="+15550001111")._builtin_phone_tools()
    assert builtin.SEND_TEXT not in _engine()._builtin_phone_tools()
    assert builtin.SEND_TEXT not in _engine(caller_number="+15550001111", sandbox=True)._builtin_phone_tools()
    off = normalize_policy({"tools": {"sms": {"mode": "off"}}})
    assert builtin.SEND_TEXT not in _engine(off, caller_number="+15550001111")._builtin_phone_tools()
    monkeypatch.delenv("SYSEVO_CALL_LIVE_URL")
    monkeypatch.delenv("SYSEVO_PRE_CALL_CHECK_URL", raising=False)
    assert builtin.SEND_TEXT not in _engine(caller_number="+15550001111")._builtin_phone_tools()


def test_the_hook_url_sits_next_to_the_other_sysevo_hooks(monkeypatch):
    for env in ("SYSEVO_AGENT_SMS_URL", "SYSEVO_CALL_LIVE_URL", "SYSEVO_MEMORY_PRE_CALL_URL", "SYSEVO_WALLET_DEBIT_URL"):
        monkeypatch.delenv(env, raising=False)
    # Production has no SYSEVO_CALL_LIVE_URL; the pre-call check is always set.
    monkeypatch.setenv("SYSEVO_PRE_CALL_CHECK_URL", "https://ref.supabase.co/functions/v1/dograh-pre-call-check")
    assert builtin.sms_hook_url() == "https://ref.supabase.co/functions/v1/dograh-agent-sms"
    monkeypatch.setenv("SYSEVO_AGENT_SMS_URL", "https://x/y")
    assert builtin.sms_hook_url() == "https://x/y"
    for env in ("SYSEVO_AGENT_SMS_URL", "SYSEVO_PRE_CALL_CHECK_URL"):
        monkeypatch.delenv(env)
    assert builtin.sms_hook_url() is None


def test_recording_control_needs_the_recorder():
    assert builtin.SET_RECORDING in _engine(audio_buffer=Mock())._builtin_phone_tools()
    assert builtin.SET_RECORDING not in _engine()._builtin_phone_tools()


@pytest.mark.asyncio
async def test_offered_tools_are_described_to_the_model_with_the_rule():
    policy = normalize_policy({"tools": {"sms": {"mode": "rule", "rule": "only booking links"}}})
    functions = await compose_functions_for_node(
        node=_NODE, custom_tool_manager=None, kb_document_uuids=[], phone_tools_policy=policy,
        builtin_phone_tools={builtin.SEND_TEXT, builtin.SET_RECORDING},
    )
    by_name = {f.name: f for f in functions}
    assert "only booking links" in by_name[builtin.SEND_TEXT].description
    assert by_name[builtin.SET_RECORDING].properties["recording"]["enum"] == ["off", "on"]
    plain = await compose_functions_for_node(node=_NODE, custom_tool_manager=None, kb_document_uuids=[])
    assert builtin.SEND_TEXT not in {f.name for f in plain}


# --- texting ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_text_goes_to_the_caller_through_sysevo_and_is_capped(monkeypatch):
    monkeypatch.setenv("SYSEVO_CALL_LIVE_URL", LIVE_URL)
    engine = _engine(caller_number="+15550001111", workflow_id=42)
    await engine._register_builtin_phone_tools(engine._builtin_phone_tools())
    send_text = _registered(engine, builtin.SEND_TEXT)
    hook = AsyncMock(return_value={"status": "sent"})
    with patch.object(builtin, "send_caller_text", hook):
        results = []
        for _ in range(builtin.MAX_TEXTS_PER_CALL + 1):
            cb = AsyncMock()
            await send_text(SimpleNamespace(arguments={"message": "  Book here:  https://x.y/b "}, result_callback=cb))
            results.append(cb.await_args.args[0]["status"])
    assert results == ["sent"] * builtin.MAX_TEXTS_PER_CALL + ["not_sent"]
    assert hook.await_args.kwargs == {"workflow_id": 42, "workflow_run_id": 5, "to": "+15550001111", "body": "Book here: https://x.y/b"}
    event = engine._logs_buffer.append.await_args.args[0]
    assert event == {"type": "phone_tool", "payload": {"tool": "sms", "outcome": "sent", "characters": 24}}
    assert "x.y" not in str(engine._logs_buffer.append.await_args_list)  # the body is never in the history


@pytest.mark.asyncio
async def test_a_text_that_cannot_go_is_explained_to_the_model(monkeypatch):
    monkeypatch.setenv("SYSEVO_CALL_LIVE_URL", LIVE_URL)
    engine = _engine(caller_number="+15550001111")
    await engine._register_builtin_phone_tools(engine._builtin_phone_tools())
    send_text = _registered(engine, builtin.SEND_TEXT)
    blocked = {"status": "blocked_opt_out", "reason": "opted_out", "readable_reason": "This person opted out of texts"}
    cb = AsyncMock()
    with patch.object(builtin, "send_caller_text", AsyncMock(return_value=blocked)):
        await send_text(SimpleNamespace(arguments={"message": "hi"}, result_callback=cb))
    sent = cb.await_args.args[0]
    assert sent["status"] == "not_sent" and "opted out" in sent["reason"] and "by voice" in sent["note"]


@pytest.mark.asyncio
async def test_the_hook_call_never_raises(monkeypatch):
    monkeypatch.setenv("SYSEVO_CALL_LIVE_URL", LIVE_URL)
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", "s")

    class Boom:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): raise RuntimeError("network down")
        async def __aexit__(self, *a): return False

    with patch.object(builtin.httpx, "AsyncClient", Boom):
        out = await builtin.send_caller_text(workflow_id=1, workflow_run_id=2, to="+1", body="x")
    assert out["status"] == "failed"
    monkeypatch.delenv("SYSEVO_MEMORY_SECRET")
    assert (await builtin.send_caller_text(workflow_id=1, workflow_run_id=2, to="+1", body="x"))["reason"] == "not_configured"


# --- recording -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pausing_and_resuming_the_recording():
    recorder = Mock(start_recording=AsyncMock(), stop_recording=AsyncMock())
    engine = _engine(audio_buffer=recorder)
    assert engine.recording_should_start()
    await engine.set_recording(False)
    await engine.set_recording(False)  # already paused: no second flush
    await engine.set_recording(True)
    assert recorder.stop_recording.await_count == 1 and recorder.start_recording.await_count == 1
    outcomes = [c.args[0]["payload"]["outcome"] for c in engine._logs_buffer.append.await_args_list]
    assert outcomes == ["paused", "resumed"]


@pytest.mark.asyncio
async def test_calls_can_start_unrecorded_until_the_caller_agrees():
    recorder = Mock(start_recording=AsyncMock(), stop_recording=AsyncMock())
    policy = normalize_policy({"tools": {"recording": {"mode": "on", "start_paused": True}}})
    engine = _engine(policy, audio_buffer=recorder)
    assert not engine.recording_should_start()
    await engine._register_builtin_phone_tools(engine._builtin_phone_tools())
    set_recording = _registered(engine, builtin.SET_RECORDING)
    cb = AsyncMock()
    await set_recording(SimpleNamespace(arguments={"recording": "on"}, result_callback=cb))
    recorder.start_recording.assert_awaited_once()
    assert cb.await_args.args[0] == {"status": "recording"}
    cb = AsyncMock()
    await set_recording(SimpleNamespace(arguments={"recording": "maybe"}, result_callback=cb))
    assert cb.await_args.args[0]["status"] == "error"
