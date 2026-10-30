"""Transfers on Vonage, and the carrier-neutral outcome rules (phone tools phase 4)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.services.telephony import warm_transfer
from api.services.telephony.transfer_event_protocol import TransferEventType
from api.tests.test_warm_transfer import FakeTransfers, _context


def _patched(store):
    return patch(
        "api.services.telephony.call_transfer_manager.get_call_transfer_manager",
        AsyncMock(return_value=store),
    )


@pytest.mark.asyncio
async def test_cold_leg_connects_on_answer_and_reports_once():
    store = FakeTransfers(_context(warm=False, introduction=None))
    with _patched(store):
        assert await warm_transfer.report_transfer_leg("t1", "answered", "leg") == "connected"
        assert await warm_transfer.report_transfer_leg("t1", "completed", "leg") == "ignored"
    assert [e.type for e in store.events] == [TransferEventType.DESTINATION_ANSWERED]
    assert store.contexts["t1"].accepted


@pytest.mark.asyncio
async def test_warm_leg_waits_for_the_key_and_a_hangup_is_a_decline():
    store = FakeTransfers(_context())
    with _patched(store):
        assert await warm_transfer.report_transfer_leg("t1", "answered") == "pending"
        assert await warm_transfer.report_transfer_leg("t1", "completed") == "declined"
        assert await warm_transfer.report_transfer_leg("t1", "busy") == "ignored"
    assert [(e.type, e.reason) for e in store.events] == [(TransferEventType.TRANSFER_FAILED, "declined")]


@pytest.mark.asyncio
async def test_warm_leg_never_answered_is_no_answer():
    store = FakeTransfers(_context())
    with _patched(store):
        assert await warm_transfer.report_transfer_leg("t1", "completed") == "no_answer"


@pytest.mark.asyncio
@pytest.mark.parametrize("digits,accepted", [("1", True), ("9", False), ("", False)])
async def test_the_key_decides(digits, accepted):
    store = FakeTransfers(_context())
    with _patched(store):
        conference = await warm_transfer.report_transfer_accept("t1", digits, "leg")
        assert await warm_transfer.report_transfer_accept("t1", "1", "leg") is None  # once
    assert (conference == "transfer-CA-caller") is accepted
    assert len(store.events) == 1


@pytest.mark.asyncio
async def test_vonage_webhooks_map_statuses_and_answer_with_an_ncco():
    from api.services.telephony.providers.vonage import routes

    class Req:
        def __init__(self, body): self._b = body
        async def json(self): return self._b

    store = FakeTransfers(_context())
    with _patched(store):
        assert await routes.vonage_transfer_result("t1", Req({"status": "ringing"})) == {"status": "ignored"}
        assert await routes.vonage_transfer_result("t1", Req({"status": "answered", "uuid": "u"})) == {"status": "pending"}
        response = await routes.vonage_transfer_accept("t1", Req({"dtmf": {"digits": "1"}, "uuid": "u"}))
    ncco = json.loads(response.body)
    assert ncco[-1] == {"action": "conversation", "name": "transfer-CA-caller", "endOnExit": True}
    assert store.events[0].type == TransferEventType.DESTINATION_ANSWERED

    store = FakeTransfers(_context())
    with _patched(store):
        response = await routes.vonage_transfer_accept("t1", Req({"dtmf": {"digits": "2"}}))
    assert all(step["action"] == "talk" for step in json.loads(response.body))  # hang up, no conversation


@pytest.mark.asyncio
async def test_vonage_dials_the_colleague_with_the_introduction():
    from api.services.telephony.providers.vonage.provider import VonageProvider

    provider = VonageProvider({"application_id": "app", "private_key": "k", "from_numbers": ["+15550009999"]})
    provider.validate_config = lambda: True
    provider._get_auth_headers = lambda: {}
    sent = {}

    class Resp:
        status = 201
        async def json(self, content_type=None): return {"uuid": "leg-1"}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def post(self, url, json, headers):
            sent.update(url=url, body=json)
            return Resp()

    with patch("api.services.telephony.providers.vonage.provider.aiohttp.ClientSession", Session), \
         patch("api.services.telephony.providers.vonage.provider.get_backend_endpoints", AsyncMock(return_value=("https://api.x", "wss://api.x"))):
        out = await provider.transfer_call("+15550001111", "t1", "transfer-c", 25, introduction="Jane, boiler")
    assert out["call_sid"] == "leg-1"
    body = sent["body"]
    assert body["to"] == [{"type": "phone", "number": "15550001111"}] and body["ringing_timer"] == 25
    assert "Jane, boiler. Press 1" in body["ncco"][0]["text"]
    assert body["ncco"][1]["eventUrl"] == ["https://api.x/api/v1/telephony/vonage/transfer-accept/t1"]
    assert body["event_url"] == ["https://api.x/api/v1/telephony/vonage/transfer-result/t1"]
    assert provider.supports_transfers() and provider.supports_warm_transfer()


@pytest.mark.asyncio
async def test_the_caller_is_moved_into_the_conversation_at_hand_off():
    from api.services.telephony.providers.vonage import strategies

    store = FakeTransfers(_context(original_call_sid="caller-uuid", accepted=True, resolved=True))
    store.find_transfer_context_for_call = AsyncMock(return_value=store.contexts["t1"])
    sent = {}

    class Resp:
        status = 204
        async def text(self): return ""
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def put(self, url, json, headers):
            sent.update(url=url, body=json)
            return Resp()

    with _patched(store), patch.object(strategies.aiohttp, "ClientSession", Session), \
         patch.object(strategies, "_jwt", lambda *a: "tok"):
        ok = await strategies.VonageConversationStrategy().execute_transfer(
            {"call_uuid": "caller-uuid", "application_id": "a", "private_key": "k"}
        )
    assert ok and sent["url"].endswith("/v1/calls/caller-uuid")
    assert sent["body"]["destination"]["ncco"][0]["name"] == "transfer-CA-caller"


@pytest.mark.asyncio
async def test_the_serializer_hands_off_instead_of_hanging_up():
    from pipecat.frames.frames import EndFrame
    from pipecat.utils.enums import EndTaskReason
    from api.services.telephony.providers.vonage.serializers import VonageFrameSerializer

    strategy = SimpleNamespace(execute_transfer=AsyncMock(return_value=True))
    serializer = VonageFrameSerializer(call_uuid="u", application_id="a", private_key="k", transfer_strategy=strategy)
    serializer._hang_up_call = AsyncMock()
    assert await serializer.serialize(EndFrame(reason=EndTaskReason.TRANSFER_CALL.value)) is None
    strategy.execute_transfer.assert_awaited_once()
    serializer._hang_up_call.assert_not_awaited()
