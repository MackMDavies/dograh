"""Transfers on Plivo and Vobiz (phone tools phase 4)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.services.telephony import xml_conference_transfer as xml
from api.services.telephony.transfer_event_protocol import TransferEventType
from api.tests.test_warm_transfer import FakeTransfers, _context

BACKEND = ("https://api.x", "wss://api.x")


class Form:
    def __init__(self, data): self._d = data
    async def form(self): return self._d


def _routes(carrier="plivo"):
    router = xml.build_router(carrier)
    return {r.path: r.endpoint for r in router.routes}


def _patched(store):
    return patch("api.services.telephony.call_transfer_manager.get_call_transfer_manager", AsyncMock(return_value=store))


def test_xml_is_escaped_and_warm_waits_for_one_key():
    warm = xml.warm_xml("Jane <Doe> & co", "https://api.x/accept?a=1&b=2")
    assert 'numDigits="1"' in warm and "a=1&amp;b=2" in warm and "Jane &lt;Doe&gt; &amp; co" in warm
    assert "<Conference" not in warm
    assert ">transfer-1</Conference>" in xml.conference_xml("transfer-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("carrier", ["plivo", "vobiz"])
async def test_warm_leg_from_answer_to_conference(carrier):
    r = _routes(carrier)
    store = FakeTransfers(_context())
    with _patched(store), patch.object(xml, "get_backend_endpoints", AsyncMock(return_value=BACKEND)):
        answer = await r[f"/{carrier}/transfer-answer/{{transfer_id}}"]("t1", Form({"CallUUID": "leg"}))
        assert "GetDigits" in answer.body.decode() and f"/telephony/{carrier}/transfer-accept/t1" in answer.body.decode()
        assert store.events == []
        accept = await r[f"/{carrier}/transfer-accept/{{transfer_id}}"]("t1", Form({"Digits": "1", "CallUUID": "leg"}))
        assert "transfer-CA-caller</Conference>" in accept.body.decode()
        join = await r[f"/{carrier}/transfer-join/{{transfer_id}}"]("t1")
        assert "transfer-CA-caller</Conference>" in join.body.decode()
    assert [e.type for e in store.events] == [TransferEventType.DESTINATION_ANSWERED]


@pytest.mark.asyncio
async def test_cold_leg_connects_on_answer_and_hangups_map_to_outcomes():
    r = _routes()
    store = FakeTransfers(_context(warm=False, introduction=None))
    with _patched(store), patch.object(xml, "get_backend_endpoints", AsyncMock(return_value=BACKEND)):
        answer = await r["/plivo/transfer-answer/{transfer_id}"]("t1", Form({}))
    assert "<Conference" in answer.body.decode() and store.events[0].type == TransferEventType.DESTINATION_ANSWERED

    store = FakeTransfers(_context(warm=False, introduction=None))
    with _patched(store):
        out = await r["/plivo/transfer-hangup/{transfer_id}"]("t1", Form({"CallStatus": "no-answer"}))
    assert out == {"status": "no_answer"} and store.events[0].reason == "no_answer"


@pytest.mark.asyncio
async def test_a_declined_warm_leg_hangs_up_and_the_caller_is_never_joined():
    r = _routes()
    store = FakeTransfers(_context())
    with _patched(store):
        accept = await r["/plivo/transfer-accept/{transfer_id}"]("t1", Form({"Digits": "3"}))
        join = await r["/plivo/transfer-join/{transfer_id}"]("t1")
    assert "<Hangup/>" in accept.body.decode() and "<Hangup/>" in join.body.decode()
    assert store.events[0].reason == "declined"


class _Session:
    def __init__(self, sent, status=201, body=None):
        self.sent, self.status, self.body = sent, status, body or {"request_uuid": "leg-1"}
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    def post(self, url, json, **auth):
        self.sent.update(url=url, body=json, auth=auth)
        outer = self

        class R:
            status = outer.status
            async def json(self, content_type=None): return outer.body
            async def text(self): return ""
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
        return R()


@pytest.mark.asyncio
@pytest.mark.parametrize("carrier,host", [("plivo", "api.plivo.com"), ("vobiz", "api.vobiz.ai")])
async def test_dialling_the_colleague(carrier, host):
    sent = {}
    with patch.object(xml.aiohttp, "ClientSession", lambda: _Session(sent)), \
         patch.object(xml, "get_backend_endpoints", AsyncMock(return_value=BACKEND)):
        out = await xml.dial_colleague(carrier, auth_id="MA1", auth_token="t", from_numbers=["+15550009999"],
                                       destination="+15550001111", transfer_id="t1", timeout=25)
    assert out["call_sid"] == "leg-1" and host in sent["url"]
    assert sent["body"]["answer_url"] == f"https://api.x/api/v1/telephony/{carrier}/transfer-answer/t1"
    assert sent["body"]["to"] == "15550001111" and sent["body"]["ring_timeout"] == 25
    assert ("headers" in sent["auth"]) is (carrier == "vobiz")


@pytest.mark.asyncio
async def test_the_caller_is_transferred_into_the_conference():
    store = FakeTransfers(_context(original_call_sid="CU-1", accepted=True, resolved=True))
    store.find_transfer_context_for_call = AsyncMock(return_value=store.contexts["t1"])
    sent = {}
    with _patched(store), patch.object(xml.aiohttp, "ClientSession", lambda: _Session(sent, status=202)), \
         patch.object(xml, "get_backend_endpoints", AsyncMock(return_value=BACKEND)):
        ok = await xml.XmlConferenceStrategy("plivo").execute_transfer({"call_id": "CU-1", "auth_id": "MA1", "auth_token": "t"})
    assert ok and sent["url"].endswith("/Account/MA1/Call/CU-1/")
    assert sent["body"] == {"legs": "aleg", "aleg_url": "https://api.x/api/v1/telephony/plivo/transfer-join/t1", "aleg_method": "POST"}


@pytest.mark.asyncio
@pytest.mark.parametrize("carrier", ["plivo", "vobiz"])
async def test_the_serializers_hand_off_instead_of_hanging_up(carrier):
    import importlib

    from pipecat.frames.frames import EndFrame
    from pipecat.utils.enums import EndTaskReason

    module = importlib.import_module(f"api.services.telephony.providers.{carrier}.serializers")
    cls = getattr(module, f"{carrier.capitalize()}FrameSerializer")
    strategy = SimpleNamespace(execute_transfer=AsyncMock(return_value=True))
    serializer = cls(stream_id="s", call_id="CU-1", auth_id="MA1", auth_token="t", transfer_strategy=strategy)
    serializer._hang_up_call = AsyncMock()
    assert await serializer.serialize(EndFrame(reason=EndTaskReason.TRANSFER_CALL.value)) is None
    strategy.execute_transfer.assert_awaited_once()
    serializer._hang_up_call.assert_not_awaited()
