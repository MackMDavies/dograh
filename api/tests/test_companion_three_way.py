"""Three-way calls on stream carriers: Dograh mixes the colleague in (phone tools phase 5)."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
import pytest

from api.services.pipecat import companion
from api.services.telephony.transfer_event_protocol import TransferEvent, TransferEventType
from api.tests.test_warm_transfer import FakeTransfers, _context, _provider, _tool, _transfer_manager


def pcm(*values):
    return np.array(values, dtype=np.int16).tobytes()


def samples(data):
    return list(np.frombuffer(data, dtype=np.int16))


def test_pcm_is_summed_and_clipped():
    assert samples(companion.add_pcm(pcm(1000, -5), pcm(2000, 5, 7))) == [3000, 0, 7]
    assert samples(companion.add_pcm(pcm(30000), pcm(30000))) == [32767]
    assert companion.add_pcm(pcm(1), b"") == pcm(1)


def test_each_person_hears_the_other_two():
    bridge = companion.CompanionBridge("t1", 8000, "redis://x")
    bridge.active = True
    # The colleague says something.
    bridge._colleague_for_caller.push(pcm(100, 100))
    bridge._colleague_for_agent.push(pcm(100, 100))
    # The agent hears caller + colleague.
    assert samples(bridge.on_caller_input(pcm(10, 20))) == [110, 120]
    # The caller hears agent + colleague; the colleague is sent agent + caller.
    out = bridge.on_output_chunk(agent_pcm=pcm(1, 2), mixed=pcm(1, 2))
    assert samples(out) == [101, 102]
    assert samples(bridge._outgoing.get_nowait()) == [11, 22]


def test_nothing_changes_without_a_colleague():
    bridge = companion.CompanionBridge("t1", 8000, "redis://x")
    assert bridge.on_caller_input(pcm(5)) == pcm(5)
    assert bridge.on_output_chunk(pcm(1), pcm(2)) == pcm(2)


def test_buffers_never_build_latency():
    buf = companion._PcmBuffer(8000)
    buf.push(b"\x01" * 20000)
    assert len(buf._data) == int(8000 * 2 * companion.MAX_BUFFER_SECONDS)


@pytest.mark.asyncio
async def test_the_mixer_wraps_only_an_existing_mixer_and_passes_through():
    from pipecat.audio.mixers.silence_mixer import SilenceAudioMixer

    transport = SimpleNamespace(_params=SimpleNamespace(audio_out_mixer=SilenceAudioMixer()))
    mixer = companion.install_companion_mixer(transport)
    assert isinstance(transport._params.audio_out_mixer, companion.CompanionMixer)
    assert companion.install_companion_mixer(transport) is mixer
    await mixer.start(8000)
    assert await mixer.mix(pcm(7)) == pcm(7)
    assert companion.install_companion_mixer(SimpleNamespace(_params=SimpleNamespace(audio_out_mixer=None))) is None


@pytest.mark.asyncio
async def test_stream_carriers_mix_the_colleague_in_and_the_agent_stays_on():
    store = FakeTransfers()
    store.outcomes = [lambda tid: TransferEvent(type=TransferEventType.DESTINATION_ANSWERED, transfer_id=tid,
                                                 original_call_sid="CA-caller", status="success", action="destination_answered")]
    provider = _provider()
    provider.supports_three_way = lambda: False
    provider.supports_companion = lambda: True
    manager, engine, patches = _transfer_manager(provider, store)
    engine.companion_available = lambda: True
    engine.start_companion = AsyncMock(return_value=True)
    handler = manager._create_transfer_call_handler(_tool(transfer_mode="three_way"), "add_colleague")
    result = AsyncMock()
    for p in patches: p.start()
    try:
        await handler(SimpleNamespace(arguments={}, result_callback=result))
    finally:
        for p in patches: p.stop()
    assert provider.transfer_call.await_args.kwargs["three_way"] is True
    engine.start_companion.assert_awaited_once()
    assert result.await_args.args[0]["status"] == "colleague_joined"
    engine.end_call_with_reason.assert_not_awaited()


@pytest.mark.asyncio
async def test_twilio_and_cloudonix_stream_the_colleague_to_the_companion_socket():
    from api.services.telephony import warm_transfer

    with patch("api.utils.common.get_backend_endpoints", AsyncMock(return_value=("https://api.x", "wss://api.x"))):
        url = await warm_transfer.companion_socket_url("t1")
    assert url == "wss://api.x/api/v1/telephony/companion/t1"
    twiml = warm_transfer.companion_stream_twiml(url)
    assert '<Stream url="wss://api.x/api/v1/telephony/companion/t1"/>' in twiml and "<Hangup/>" in twiml


@pytest.mark.asyncio
async def test_plivo_answer_streams_a_three_way_colleague():
    from api.services.telephony import xml_conference_transfer as xml

    store = FakeTransfers(_context(three_way=True, warm=False, introduction=None))
    routes = {r.path: r.endpoint for r in xml.build_router("plivo").routes}

    class Form:
        async def form(self): return {"CallUUID": "leg"}

    with patch("api.services.telephony.call_transfer_manager.get_call_transfer_manager", AsyncMock(return_value=store)), \
         patch("api.utils.common.get_backend_endpoints", AsyncMock(return_value=("https://api.x", "wss://api.x"))):
        body = (await routes["/plivo/transfer-answer/{transfer_id}"]("t1", Form())).body.decode()
    assert '<Stream bidirectional="true"' in body and "wss://api.x/api/v1/telephony/companion/t1" in body
    assert store.events[0].type == TransferEventType.DESTINATION_ANSWERED


@pytest.mark.asyncio
async def test_the_companion_handshake_reads_each_carriers_stream_ids():
    from api.services.telephony import companion_ws

    class WS:
        def __init__(self, *messages): self._m = list(messages)
        async def receive_text(self): return json.dumps(self._m.pop(0))

    twilio = await companion_ws._handshake("twilio", WS({"event": "connected"}, {"event": "start", "start": {"streamSid": "MZ", "callSid": "CA"}}), None)
    assert twilio == {"stream_sid": "MZ", "call_sid": "CA"}
    plivo = await companion_ws._handshake("plivo", WS({"event": "start", "start": {"streamId": "s", "callId": "c"}}), None)
    assert plivo == {"stream_id": "s", "call_id": "c"}
    telnyx = await companion_ws._handshake("telnyx", WS({"event": "connected"}, {"event": "start", "stream_id": "s", "start": {"call_control_id": "cc"}}), None)
    assert telnyx["call_control_id"] == "cc" and telnyx["encoding"] == "PCMU"
    assert await companion_ws._handshake("vonage", WS(), "uuid-1") == {"call_uuid": "uuid-1"}
    assert await companion_ws._handshake("twilio", WS({"event": "media"}), None) is None


class FakeRedis:
    """Enough of redis.asyncio for one process: publish fans out to subscribers."""

    channels: dict = {}
    published: list = []

    @classmethod
    def from_url(cls, url): return cls()

    async def publish(self, channel, data):
        FakeRedis.published.append((channel, data))
        for queue in FakeRedis.channels.get(channel, []):
            await queue.put({"type": "message", "channel": channel.encode(), "data": data})

    def pubsub(self):
        outer = self

        class PubSub:
            def __init__(self): self.q = asyncio.Queue()
            async def subscribe(self, *names):
                for name in names:
                    FakeRedis.channels.setdefault(name, []).append(self.q)
            async def listen(self):
                while True:
                    yield await self.q.get()
            async def aclose(self): pass
        return PubSub()

    async def aclose(self): pass


@pytest.mark.asyncio
async def test_the_companion_socket_relays_both_ways_and_reports_leaving():
    from pipecat.frames.frames import InputAudioRawFrame
    from api.services.telephony import companion_ws

    FakeRedis.channels, FakeRedis.published = {}, []
    store = FakeTransfers(_context(three_way=True, warm=False, introduction=None, call_sid="CA-leg"))

    class Serializer:
        _params = SimpleNamespace(auto_hang_up=True)
        async def setup(self, frame): self.rate = frame.audio_out_sample_rate
        async def deserialize(self, data): return InputAudioRawFrame(audio=pcm(9), sample_rate=8000, num_channels=1)
        async def serialize(self, frame): return json.dumps({"media": [int(x) for x in samples(frame.audio)]})

    serializer = Serializer()
    incoming = asyncio.Queue()
    sent = []

    class WS:
        async def accept(self): pass
        async def receive_text(self):
            return json.dumps(incoming_handshake.pop(0))
        async def receive(self): return await incoming.get()
        async def send_text(self, text): sent.append(text)
        async def send_bytes(self, data): sent.append(data)
        async def close(self, code=1000, reason=""): pass

    incoming_handshake = [{"event": "connected"}, {"event": "start", "start": {"streamSid": "MZ", "callSid": "CA-leg"}}]
    run = SimpleNamespace(id=9, mode="twilio", workflow_id=3, initial_context={})
    spec = SimpleNamespace(transport_factory=AsyncMock(return_value=SimpleNamespace(_params=SimpleNamespace(serializer=serializer))))
    from api.db import db_client

    with patch("api.services.telephony.call_transfer_manager.get_call_transfer_manager", AsyncMock(return_value=store)), \
         patch.object(db_client, "get_workflow_run_by_id", AsyncMock(return_value=run)), \
         patch.object(db_client, "get_workflow_by_id", AsyncMock(return_value=SimpleNamespace(organization_id=1))), \
         patch("api.services.telephony.registry.get", lambda name: spec), \
         patch("redis.asyncio.from_url", FakeRedis.from_url):
        socket = asyncio.create_task(companion_ws.companion_socket(WS(), "t1"))
        await asyncio.sleep(0.05)
        await incoming.put({"type": "websocket.receive", "text": "{}"})          # the colleague speaks
        await FakeRedis().publish(companion.down_channel("t1"), pcm(4, 5))        # the call sends audio
        await asyncio.sleep(0.05)
        await incoming.put({"type": "websocket.disconnect"})                     # the colleague hangs up
        await asyncio.wait_for(socket, timeout=2)

    assert serializer._params.auto_hang_up is False
    assert (companion.up_channel("t1"), pcm(9)) in FakeRedis.published
    assert json.loads(sent[0]) == {"media": [4, 5]}
    assert (companion.control_channel("t1"), b"left") in FakeRedis.published
