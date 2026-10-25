"""Payment links and hold music (Keypad & Native Phone Tools, phase 4)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from api.services.phone_tools import builtin
from api.services.phone_tools.policy import normalize_policy
from api.services.workflow.pipecat_engine import PipecatEngine
from api.services.workflow.pipecat_engine_context_composer import compose_functions_for_node

LINKS = [
    {"slug": "dep-1", "title": "Booking deposit", "amount": 5000, "currency": "usd"},
    {"slug": "tip-1", "title": "Pay what you like", "amount": None, "currency": "usd"},
]
_NODE = SimpleNamespace(document_uuids=[], tool_uuids=[], out_edges=[])


def test_the_agent_can_only_offer_the_businesss_own_links():
    description, properties = builtin.payment_link_schema(LINKS)
    assert properties["link"]["enum"] == ["Booking deposit", "Pay what you like"]
    assert "50.00 USD" in description and "amount chosen by the payer" in description
    assert "Never ask for or accept card numbers" in description


@pytest.mark.asyncio
async def test_payment_links_are_offered_once_loaded_and_sent_by_name(monkeypatch):
    monkeypatch.setenv("SYSEVO_PRE_CALL_CHECK_URL", "https://ref.supabase.co/functions/v1/dograh-pre-call-check")
    engine = PipecatEngine(llm=Mock(register_function=Mock()), workflow=Mock(), call_context_vars={}, workflow_run_id=5)
    with patch.object(builtin, "list_payment_links", AsyncMock(return_value=LINKS)):
        engine.set_phone_tools(normalize_policy(None), logs_buffer=Mock(append=AsyncMock()),
                               caller_number="+15550001111", workflow_id=42)
        await engine._collect_payment_links()
    names = engine._builtin_phone_tools()
    assert builtin.SEND_PAYMENT_LINK in names
    functions = await compose_functions_for_node(
        node=_NODE, custom_tool_manager=None, kb_document_uuids=[], phone_tools_policy=engine._phone_tools,
        builtin_phone_tools=names, payment_links=engine._payment_links,
    )
    assert builtin.SEND_PAYMENT_LINK in {f.name for f in functions}

    await engine._register_builtin_phone_tools(names)
    handler = next(c.args[1] for c in engine.llm.register_function.call_args_list if c.args[0] == builtin.SEND_PAYMENT_LINK)
    sender = AsyncMock(return_value={"status": "sent"})
    with patch.object(builtin, "send_payment_link", sender):
        cb = AsyncMock()
        await handler(SimpleNamespace(arguments={"link": "booking deposit"}, result_callback=cb))
        assert cb.await_args.args[0]["status"] == "sent"
        assert sender.await_args.kwargs["slug"] == "dep-1"
        cb = AsyncMock()
        await handler(SimpleNamespace(arguments={"link": "a free car"}, result_callback=cb))
        assert cb.await_args.args[0]["status"] == "error" and sender.await_count == 1


@pytest.mark.asyncio
async def test_no_links_or_switched_off_means_no_tool(monkeypatch):
    monkeypatch.setenv("SYSEVO_PRE_CALL_CHECK_URL", "https://ref.supabase.co/functions/v1/dograh-pre-call-check")
    for policy, links in ((normalize_policy(None), []), (normalize_policy({"tools": {"payments": {"mode": "off"}}}), LINKS)):
        engine = PipecatEngine(llm=Mock(), workflow=Mock(), call_context_vars={}, workflow_run_id=5)
        with patch.object(builtin, "list_payment_links", AsyncMock(return_value=links)):
            engine.set_phone_tools(policy, caller_number="+15550001111", workflow_id=42)
            await engine._collect_payment_links()
        assert builtin.SEND_PAYMENT_LINK not in engine._builtin_phone_tools()


def _manager(hold_on=True):
    from api.services.workflow.pipecat_engine_custom_tools import CustomToolManager

    engine = Mock()
    engine.hold_music_allowed = lambda: hold_on
    engine._audio_config = None
    engine._transport_output = Mock(queue_frame=AsyncMock())
    engine.log_phone_tool_event = AsyncMock()
    return CustomToolManager(engine), engine


@pytest.mark.asyncio
async def test_a_slow_lookup_plays_hold_music_until_it_returns(monkeypatch):
    from api.services.workflow import pipecat_engine_custom_tools as module

    monkeypatch.setattr(builtin, "HOLD_AFTER_SECONDS", 0.05)
    played = {}

    async def fake_loop(*, stop_event, sample_rate, queue_frame):
        played["started"] = True
        await stop_event.wait()
        played["stopped"] = True

    monkeypatch.setattr(module, "play_audio_loop", fake_loop)
    manager, engine = _manager()

    async def slow(params):
        await asyncio.sleep(0.2)

    await manager._with_hold_music(slow)(SimpleNamespace())
    assert played == {"started": True, "stopped": True}
    engine.log_phone_tool_event.assert_awaited_with("hold", outcome="started")

    played.clear()

    async def quick(params):
        return None

    await manager._with_hold_music(quick)(SimpleNamespace())
    assert played == {}  # no music for a lookup that answers at once


@pytest.mark.asyncio
async def test_hold_music_off_runs_the_lookup_plainly(monkeypatch):
    from api.services.workflow import pipecat_engine_custom_tools as module

    loop = AsyncMock()
    monkeypatch.setattr(module, "play_audio_loop", loop)
    manager, _ = _manager(hold_on=False)
    ran = []

    async def slow(params):
        ran.append(1)

    await manager._with_hold_music(slow)(SimpleNamespace())
    assert ran == [1] and loop.await_count == 0
