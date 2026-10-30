"""Live, speaker-attributed captions for Twilio dialer calls (contract C1)."""
import asyncio
import base64
import json

import pytest

from api.services.telephony.dialer import twilio_live_transcribe as tlt
from api.services.telephony.dialer.live_transcribe import caption_payload

SECRET = "test-auth-token"
KEY = "CA" + "a" * 32
LEG = KEY
NOW = 1_800_000_000


@pytest.fixture(autouse=True)
def _deepgram(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.delenv("SYSEVO_DIALER_LIVE_TRANSCRIBE", raising=False)


# ── token ──────────────────────────────────────────────────────────────────────

def test_token_round_trips_for_the_same_call_and_leg():
    token = tlt.issue_stream_token(secret=SECRET, channel_key=KEY, leg_call_sid=LEG, now=NOW)
    assert tlt.verify_stream_token(secret=SECRET, channel_key=KEY, leg_call_sid=LEG, token=token, now=NOW + 5)


@pytest.mark.parametrize("change", ["key", "leg", "secret", "expired", "tampered", "garbage", "far_future"])
def test_token_is_rejected_when_anything_differs(change):
    token = tlt.issue_stream_token(secret=SECRET, channel_key=KEY, leg_call_sid=LEG, now=NOW)
    kwargs = dict(secret=SECRET, channel_key=KEY, leg_call_sid=LEG, token=token, now=NOW + 1)
    if change == "key":
        kwargs["channel_key"] = "CA" + "b" * 32
    elif change == "leg":
        kwargs["leg_call_sid"] = "CA" + "c" * 32
    elif change == "secret":
        kwargs["secret"] = "other"
    elif change == "expired":
        kwargs["now"] = NOW + tlt.TOKEN_TTL_SECONDS + 1
    elif change == "tampered":
        kwargs["token"] = token[:-1] + ("0" if token[-1] != "0" else "1")
    elif change == "garbage":
        kwargs["token"] = "not-a-token"
    elif change == "far_future":
        exp = NOW + 10_000
        kwargs["token"] = f"{exp}.{tlt._signature(SECRET, KEY, LEG, exp)}"
    assert not tlt.verify_stream_token(**kwargs)


def test_no_token_without_a_secret():
    assert tlt.issue_stream_token(secret="", channel_key=KEY, leg_call_sid=LEG) == ""


# ── TwiML ──────────────────────────────────────────────────────────────────────

def test_stream_twiml_targets_the_secure_endpoint_with_both_tracks():
    twiml = tlt.stream_twiml(backend_endpoint="https://api.example.com/", channel_key=KEY,
                             leg_call_sid=LEG, secret=SECRET, now=NOW)
    assert twiml.startswith("<Start><Stream ")
    assert 'url="wss://api.example.com/api/v1/telephony/dialer-twilio-transcribe"' in twiml
    assert 'track="both_tracks"' in twiml
    assert f'<Parameter name="channel_key" value="{KEY}"/>' in twiml
    assert '<Parameter name="token" value="' in twiml


@pytest.mark.parametrize("endpoint,secret,key,flag", [
    ("http://api.example.com", SECRET, "dg", None),       # never plain http
    ("/api/v1", SECRET, "dg", None),                       # unresolved endpoint
    ("https://api.example.com", "", "dg", None),           # no secret
    ("https://api.example.com", SECRET, "", None),         # no Deepgram key
    ("https://api.example.com", SECRET, "dg", "off"),      # kill switch
])
def test_stream_twiml_is_omitted_when_not_configured(monkeypatch, endpoint, secret, key, flag):
    monkeypatch.setenv("DEEPGRAM_API_KEY", key)
    if flag:
        monkeypatch.setenv("SYSEVO_DIALER_LIVE_TRANSCRIBE", flag)
    assert tlt.stream_twiml(backend_endpoint=endpoint, channel_key=KEY, leg_call_sid=LEG, secret=secret) == ""


# ── media routing and payloads ─────────────────────────────────────────────────

def test_decode_media_maps_tracks_to_speakers():
    audio = b"\xff\x7f\x00"
    frame = lambda track: {"event": "media", "media": {"track": track, "payload": base64.b64encode(audio).decode()}}
    assert tlt.decode_media(frame("inbound")) == ("rep", audio)
    assert tlt.decode_media(frame("outbound")) == ("prospect", audio)
    assert tlt.decode_media(frame("mixed")) == (None, b"")
    assert tlt.decode_media({"event": "media", "media": {"track": "inbound", "payload": "%%%"}}) == (None, b"")


def test_caption_payload_shape():
    payload = caption_payload({"is_final": True, "start": 3.2, "duration": "1.5"}, "Hi there", "prospect", "twilio_stream")
    assert payload == {"type": "transcript", "text": "Hi there", "is_final": True, "start": 3.2,
                       "duration": 1.5, "speaker": "prospect", "source": "twilio_stream"}
    mono = caption_payload({"is_final": False}, "x", None, "signalwire_tap")
    assert mono["speaker"] is None and mono["start"] is None and mono["duration"] is None


class FakeTranscriber:
    instances: list["FakeTranscriber"] = []

    def __init__(self, call_id, on_text, *, speaker=None, source=None):
        self.speaker, self.source, self.on_text, self.fed = speaker, source, on_text, []
        FakeTranscriber.instances.append(self)

    enabled = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def feed(self, frame):
        self.fed.append(frame)
        await self.on_text(caption_payload({"is_final": True, "start": 1.0, "duration": 0.5},
                                           f"words from {self.speaker}", self.speaker, self.source))


class FakeSocket:
    def __init__(self, messages):
        self.messages = [json.dumps(m) for m in messages]
        self.accepted = False
        self.closed = None

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        if not self.messages:
            raise RuntimeError("disconnected")
        return self.messages.pop(0)

    async def close(self, code=1000, reason=""):
        self.closed = code


def _start(token, call_sid=LEG):
    return {"event": "start", "start": {"callSid": call_sid, "tracks": ["inbound", "outbound"],
                                         "customParameters": {"channel_key": KEY, "token": token}}}


def _media(track):
    return {"event": "media", "media": {"track": track, "payload": base64.b64encode(b"\x01\x02").decode()}}


def _run(socket, published):
    async def publish(key, payload):
        published.append((key, payload))

    async def secret():
        return SECRET

    asyncio.run(tlt.serve_twilio_transcription(socket, secret, publish=publish, transcriber_factory=FakeTranscriber))


def test_stream_routes_each_track_to_its_speaker_and_publishes_on_the_call_channel():
    FakeTranscriber.instances = []
    token = tlt.issue_stream_token(secret=SECRET, channel_key=KEY, leg_call_sid=LEG)
    socket = FakeSocket([{"event": "connected"}, _start(token), _media("inbound"), _media("outbound"),
                         _media("inbound"), {"event": "stop"}])
    published = []
    _run(socket, published)
    by_speaker = {t.speaker: t for t in FakeTranscriber.instances}
    assert len(by_speaker["rep"].fed) == 2
    assert len(by_speaker["prospect"].fed) == 1
    assert all(t.source == "twilio_stream" for t in FakeTranscriber.instances)
    assert [k for k, _ in published] == [KEY, KEY, KEY]
    assert [p["speaker"] for _, p in published] == ["rep", "prospect", "rep"]
    assert socket.closed is None


@pytest.mark.parametrize("bad", ["token", "leg"])
def test_stream_with_bad_auth_is_closed_and_publishes_nothing(bad):
    FakeTranscriber.instances = []
    token = tlt.issue_stream_token(secret=SECRET, channel_key=KEY, leg_call_sid=LEG)
    start = _start("1.deadbeef") if bad == "token" else _start(token, call_sid="CA" + "f" * 32)
    socket = FakeSocket([{"event": "connected"}, start, _media("inbound"), {"event": "stop"}])
    published = []
    _run(socket, published)
    assert socket.closed == 4401
    assert published == [] and FakeTranscriber.instances == []
