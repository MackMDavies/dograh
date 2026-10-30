"""The call sorter's session, stage seam, websocket auth and provider snippets."""

import asyncio
import base64
import json

import numpy as np
import pytest

from api.services.telephony.dialer import call_sorter as cs
from api.services.telephony.dialer import signalwire_call_sorter as swcs
from api.services.telephony.dialer import twilio_live_transcribe as tlt
from api.services.telephony.dialer.call_sorter_audio import AudioWatcher, pcm_to_ulaw
from api.services.telephony.dialer.call_sorter_rules import SorterVerdict
from api.services.telephony.dialer.swml import (
    build_dialer_swml,
    build_signalwire_parallel_leg_swml,
)
from api.services.telephony.providers.twilio import dialer_call_sorter as tcs

SECRET = "test-auth-token"
CALL_REF = "CA" + "a" * 32
LEG = "CA" + "b" * 32
NOW = 1_800_000_000
RATE = 8000


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setenv("SYSEVO_DIALER_CALL_SORTER", "1")
    monkeypatch.delenv("SYSEVO_DIALER_SORTER_LLM", raising=False)
    monkeypatch.setenv("SIGNALWIRE_API_TOKEN", "sw-test-token")
    monkeypatch.delenv("SIGNALWIRE_WEBHOOK_KEY", raising=False)
    cs.register_stage_recorder(None)
    yield
    cs.register_stage_recorder(None)


# ── audio helpers ───────────────────────────────────────────────────────────────

def _speech(seconds: float, seed: int = 1) -> bytes:
    """Loud, non-tonal audio: counts as talking, never as a beep."""
    rng = np.random.default_rng(seed)
    return pcm_to_ulaw(3000 * rng.standard_normal(int(seconds * RATE)))


def _silence(seconds: float) -> bytes:
    return pcm_to_ulaw(np.zeros(int(seconds * RATE)))


def _tone(seconds: float, freq: float = 1000.0) -> bytes:
    t = np.arange(int(seconds * RATE)) / RATE
    return pcm_to_ulaw(8000 * np.sin(2 * np.pi * freq * t))


def _frames(audio: bytes, size: int = 160):
    for i in range(0, len(audio), size):
        yield audio[i:i + size]


class Recorder:
    def __init__(self):
        self.calls = []
        self.state = []

    async def emit(self, call_ref, stage, detail):
        self.calls.append((call_ref, stage, detail))

    async def publish(self, call_ref, stage, detail):
        self.state.append((call_ref, stage))

    @property
    def stages(self):
        return [stage for _, stage, _ in self.calls]


def _sorter(rec, **kwargs):
    return cs.CallSorter(CALL_REF, emit=rec.emit, publish_state=rec.publish, provider="test", **kwargs)


async def _feed(sorter, audio: bytes):
    for frame in _frames(audio):
        await sorter.on_audio(frame)


async def _say(sorter, text, final=True):
    await sorter.on_caption({"type": "transcript", "text": text, "is_final": final})


# ── audio watcher ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("freq", [440.0, 850.0, 1000.0, 1400.0, 2000.0])
def test_beep_is_detected_at_common_mailbox_frequencies(freq):
    watcher = AudioWatcher()
    watcher.feed(_silence(0.1))
    beep = watcher.feed(_tone(0.3, freq))
    assert beep is not None
    assert abs(beep.frequency_hz - freq) <= 30


def test_speech_and_a_held_vowel_are_not_beeps():
    watcher = AudioWatcher()
    assert watcher.feed(_speech(2.0)) is None
    t = np.arange(2 * RATE) / RATE
    vowel = sum((3000 / k) * np.sin(2 * np.pi * 180 * k * t) for k in range(1, 10))
    assert AudioWatcher().feed(pcm_to_ulaw(vowel)) is None


def test_silence_is_measured_from_the_last_speech():
    watcher = AudioWatcher()
    assert watcher.silence_seconds == 0.0
    watcher.feed(_speech(0.5))
    watcher.feed(_silence(2.0))
    assert watcher.heard_speech
    assert 1.9 <= watcher.silence_seconds <= 2.1


def test_malformed_audio_is_ignored():
    watcher = AudioWatcher()
    assert watcher.feed(b"") is None
    assert watcher.feed(b"\x00") is None


# ── session: stages ─────────────────────────────────────────────────────────────

async def test_a_receptionist_is_a_person_and_ends_the_stream():
    rec = Recorder()
    sorter = _sorter(rec)
    await _feed(sorter, _speech(1.0))
    await _say(sorter, "Thank you for calling Acme Systems. This is Sarah. How can I help you?")
    assert rec.stages == ["person"]
    detail = rec.calls[0][2]
    assert detail["name"] == "Sarah"
    assert detail["gatekeeper"] is True
    assert detail["source"] == "call_sorter"
    assert sorter.done and sorter.stop_reason == "person"
    assert rec.state == [(CALL_REF, "person")]
    # Nothing is sorted once a person is on.
    await _say(sorter, "Please leave a message after the tone.")
    assert rec.stages == ["person"]


async def test_a_short_hello_becomes_a_person_only_after_the_quiet():
    rec = Recorder()
    sorter = _sorter(rec)
    await _feed(sorter, _speech(0.6))
    await _say(sorter, "Hello. This is James.")
    assert rec.stages == []
    await _feed(sorter, _silence(1.0))
    assert rec.stages == []
    await _feed(sorter, _silence(0.8))
    assert rec.stages == ["person"]
    assert rec.calls[0][2]["gatekeeper"] is False


async def test_greeting_then_beep():
    rec = Recorder()
    sorter = _sorter(rec)
    await _feed(sorter, _speech(2.0))
    await _say(sorter, "Hi, you've reached Jordan. Please leave a message after the tone.")
    assert rec.stages == ["machine"]
    assert rec.calls[0][2]["end_of_greeting_cue"] is True
    assert not sorter.done
    await _feed(sorter, _silence(0.3) + _tone(0.4))
    assert rec.stages == ["machine", "beep"]
    assert rec.calls[1][2]["source"] == "tone"
    assert sorter.done and sorter.stop_reason == "beep"


async def test_a_beep_heard_before_the_words_land_still_counts():
    rec = Recorder()
    sorter = _sorter(rec)
    await _feed(sorter, _speech(0.5))
    await _say(sorter, "You've reached", final=False)
    await _feed(sorter, _speech(2.0) + _tone(0.4))
    await _say(sorter, "You've reached Jordan. At the tone, please record your message.")
    assert rec.stages == ["machine", "beep"]
    assert rec.calls[1][2]["heard_before_verdict"] is True


async def test_mailbox_full_after_the_greeting():
    rec = Recorder()
    sorter = _sorter(rec)
    await _feed(sorter, _speech(1.0))
    await _say(sorter, "Please leave your message for (555) 010-0000.")
    await _say(sorter, "Sorry. Mailbox is full.")
    assert rec.stages == ["machine", "mailbox_full"]
    assert sorter.done


async def test_iphone_screener_that_gives_up_then_beeps():
    rec = Recorder()
    sorter = _sorter(rec)
    await _feed(sorter, _speech(1.0))
    await _say(sorter, "Hi. If you record your name and reason for calling, I'll see if this person is available.")
    assert rec.stages == ["screener"]
    assert rec.calls[0][2]["vendor"] == "iphone"
    await _say(sorter, "I'm sorry. This person is not available. If you would like to leave an additional message, please reply after the tone.")
    assert rec.stages == ["screener", "machine"]
    assert rec.calls[1][2]["kind"] == "screener_unavailable"
    await _feed(sorter, _tone(0.4, 850))
    assert rec.stages == ["screener", "machine", "beep"]


async def test_menu_then_a_person():
    rec = Recorder()
    sorter = _sorter(rec)
    await _feed(sorter, _speech(1.0))
    await _say(sorter, "Thank you for calling Acme. For sales, press 1.")
    await _say(sorter, "Acme, this is Dana. How can I help you?")
    assert rec.stages == ["ivr", "person"]


async def test_stages_only_move_forward():
    rec = Recorder()
    sorter = _sorter(rec)
    await sorter._apply(SorterVerdict("voicemail", 0.9, "x"))
    await sorter._apply(SorterVerdict("screener", 0.9, "x"))
    await sorter._apply(SorterVerdict("ivr", 0.9, "x"))
    await sorter._apply(SorterVerdict("person", 0.9, "x"))
    await sorter._apply(SorterVerdict("voicemail", 0.9, "x"))
    assert rec.stages == ["machine"]


async def test_no_beep_stops_listening(monkeypatch):
    monkeypatch.setattr(cs, "BEEP_WAIT_SECONDS", 1.0)
    rec = Recorder()
    sorter = _sorter(rec)
    await _feed(sorter, _speech(0.5))
    await _say(sorter, "You've reached Jordan. Leave me a message.")
    await _feed(sorter, _silence(1.2))
    assert sorter.done and sorter.stop_reason == "no_beep"
    assert rec.stages == ["machine"]


async def test_listening_is_capped():
    rec = Recorder()
    sorter = _sorter(rec, max_listen_seconds=1.0)
    await _feed(sorter, _speech(1.2))
    assert sorter.done and sorter.stop_reason == "listen_cap"


# ── optional LLM fallback ───────────────────────────────────────────────────────

async def _unsure_call(sorter):
    await _feed(sorter, _speech(0.5))
    await _say(sorter, "Mmm hmm and so on and so forth")
    await _feed(sorter, _speech(4.2))
    if sorter._llm_task is not None:
        await sorter._llm_task


async def test_llm_fallback_is_off_without_the_flag():
    asked = []

    async def llm(text):
        asked.append(text)
        return SorterVerdict("voicemail", 0.9, "x")

    rec = Recorder()
    sorter = _sorter(rec, llm_fallback=llm)
    await _unsure_call(sorter)
    assert asked == [] and rec.stages == []


async def test_llm_fallback_answers_an_unsure_call(monkeypatch):
    monkeypatch.setenv("SYSEVO_DIALER_SORTER_LLM", "1")

    async def llm(text):
        return SorterVerdict("voicemail", 0.99, "greeting")

    rec = Recorder()
    sorter = _sorter(rec, llm_fallback=llm)
    await _unsure_call(sorter)
    assert rec.stages == ["machine"]
    assert rec.calls[0][2]["rule"] == "llm:greeting"
    assert rec.calls[0][2]["confidence"] <= 0.8


async def test_llm_fallback_has_a_budget(monkeypatch):
    monkeypatch.setenv("SYSEVO_DIALER_SORTER_LLM", "1")
    monkeypatch.setattr(cs, "LLM_BUDGET_SECONDS", 0.05)

    async def slow(text):
        await asyncio.sleep(5)
        return SorterVerdict("person", 0.9, "x")

    rec = Recorder()
    sorter = _sorter(rec, llm_fallback=slow)
    await _unsure_call(sorter)
    assert rec.stages == []


# ── the seam ────────────────────────────────────────────────────────────────────

async def test_record_call_stage_uses_the_registered_recorder():
    seen = []

    async def recorder(call_ref, stage, detail):
        seen.append((call_ref, stage, detail))

    cs.register_stage_recorder(recorder)
    await cs.record_call_stage(CALL_REF, "screener", {"vendor": "iphone"})
    assert seen == [(CALL_REF, "screener", {"vendor": "iphone"})]


async def test_record_call_stage_refuses_unknown_stages_and_never_raises():
    seen = []

    async def recorder(call_ref, stage, detail):
        seen.append(stage)
        raise RuntimeError("db down")

    cs.register_stage_recorder(recorder)
    await cs.record_call_stage(CALL_REF, "vm_left", {})
    await cs.record_call_stage(CALL_REF, "person", {})
    assert seen == ["person"]


async def test_record_call_stage_without_a_recorder_is_a_harmless_stub(monkeypatch):
    monkeypatch.setattr(cs, "_discover_recorder", lambda: None)
    await cs.record_call_stage(CALL_REF, "ivr", {"kind": "menu"})


def test_the_stage_event_recorder_is_discovered_by_name():
    from api.services.telephony.dialer import call_stage_events

    assert cs._discover_recorder() is call_stage_events.record_call_stage


def test_stage_names_are_the_contract_names():
    assert set(cs.SORTER_STAGES) == {"person", "machine", "beep", "mailbox_full", "screener", "ivr"}


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.queues = []

    async def set(self, key, value, ex=None):
        self.values[key] = value

    async def get(self, key):
        return self.values.get(key)

    async def publish(self, key, value):
        for channel, queue in self.queues:
            if channel == key:
                queue.put_nowait({"type": "message", "data": value})

    def pubsub(self):
        redis = self

        class PubSub:
            def __init__(self):
                self.queue = asyncio.Queue()

            async def subscribe(self, key):
                redis.queues.append((key, self.queue))

            async def get_message(self, ignore_subscribe_messages=True, timeout=0.5):
                try:
                    return await asyncio.wait_for(self.queue.get(), timeout)
                except asyncio.TimeoutError:
                    return None

            async def unsubscribe(self, key):
                pass

            async def aclose(self):
                pass

        return PubSub()


async def test_wait_for_sorter_stage_returns_what_is_already_there():
    redis = FakeRedis()
    await cs.publish_sorter_state(CALL_REF, "machine", {"kind": "voicemail"}, client=redis)
    state = await cs.wait_for_sorter_stage(CALL_REF, timeout=0.2, client=redis)
    assert state["stage"] == "machine"


async def test_wait_for_sorter_stage_wakes_on_a_new_stage():
    redis = FakeRedis()

    async def later():
        await asyncio.sleep(0.05)
        await cs.publish_sorter_state(CALL_REF, "ivr", {}, client=redis)
        await cs.publish_sorter_state(CALL_REF, "person", {"name": "Sam"}, client=redis)

    task = asyncio.create_task(later())
    state = await cs.wait_for_sorter_stage(CALL_REF, timeout=1.0, client=redis)
    await task
    assert state["stage"] == "person"


async def test_wait_for_sorter_stage_times_out_quietly():
    assert await cs.wait_for_sorter_stage(CALL_REF, timeout=0.1, client=FakeRedis()) is None


# ── websocket credential ────────────────────────────────────────────────────────

def test_token_round_trip():
    token = cs.issue_sorter_token(secret=SECRET, call_ref=CALL_REF, leg_id=LEG, now=NOW)
    assert cs.verify_sorter_token(secret=SECRET, call_ref=CALL_REF, leg_id=LEG, token=token, now=NOW + 5)


@pytest.mark.parametrize("change", ["call_ref", "leg", "secret", "expired", "tampered", "garbage", "far_future"])
def test_token_is_rejected_when_anything_differs(change):
    token = cs.issue_sorter_token(secret=SECRET, call_ref=CALL_REF, leg_id=LEG, now=NOW)
    kwargs = dict(secret=SECRET, call_ref=CALL_REF, leg_id=LEG, token=token, now=NOW + 1)
    if change == "call_ref":
        kwargs["call_ref"] = "CA" + "c" * 32
    elif change == "leg":
        kwargs["leg_id"] = "CA" + "d" * 32
    elif change == "secret":
        kwargs["secret"] = "other"
    elif change == "expired":
        kwargs["now"] = NOW + cs.TOKEN_TTL_SECONDS + 1
    elif change == "tampered":
        kwargs["token"] = token[:-1] + ("0" if token[-1] != "0" else "1")
    elif change == "garbage":
        kwargs["token"] = "nope"
    elif change == "far_future":
        exp = NOW + 10_000
        kwargs["token"] = f"{exp}.{cs._signature(SECRET, CALL_REF, LEG, exp)}"
    assert not cs.verify_sorter_token(**kwargs)


def test_a_caption_stream_token_cannot_open_the_sorter():
    caption = tlt.issue_stream_token(secret=SECRET, channel_key=CALL_REF, leg_call_sid=LEG, now=NOW)
    assert not cs.verify_sorter_token(secret=SECRET, call_ref=CALL_REF, leg_id=LEG, token=caption, now=NOW + 1)


# ── Twilio snippets ─────────────────────────────────────────────────────────────

def test_twiml_forks_only_the_prospect_track_to_the_secure_endpoint():
    twiml = tcs.start_stream_twiml(
        backend_endpoint="https://api.example.com/", call_ref=CALL_REF,
        leg_call_sid=LEG, secret=SECRET, now=NOW,
    )
    assert twiml.startswith("<Start><Stream ")
    assert 'url="wss://api.example.com/api/v1/telephony/dialer-call-sorter"' in twiml
    assert 'track="inbound_track"' in twiml
    assert f'name="sorter-{LEG}"' in twiml
    assert f'<Parameter name="call_ref" value="{CALL_REF}"/>' in twiml
    token = twiml.split('name="token" value="')[1].split('"')[0]
    assert cs.verify_sorter_token(secret=SECRET, call_ref=CALL_REF, leg_id=LEG, token=token, now=NOW + 1)


@pytest.mark.parametrize("case", ["disabled", "no_deepgram", "http", "no_secret"])
def test_twiml_is_empty_whenever_it_cannot_be_safe(case, monkeypatch):
    kwargs = dict(backend_endpoint="https://api.example.com", call_ref=CALL_REF, leg_call_sid=LEG, secret=SECRET)
    if case == "disabled":
        monkeypatch.delenv("SYSEVO_DIALER_CALL_SORTER")
    elif case == "no_deepgram":
        monkeypatch.delenv("DEEPGRAM_API_KEY")
    elif case == "http":
        kwargs["backend_endpoint"] = "http://api.example.com"
    elif case == "no_secret":
        kwargs["secret"] = ""
    assert tcs.start_stream_twiml(**kwargs) == ""


def test_stop_twiml_names_the_fork():
    assert tcs.stop_stream_twiml(leg_call_sid=LEG) == f'<Stop><Stream name="sorter-{LEG}"/></Stop>'


class FakeTwilio:
    def __init__(self, fail=False):
        self.created = []
        self.updated = []
        self.fail = fail

    def calls(self, sid):
        client = self

        class Streams:
            def create(self, **params):
                if client.fail:
                    raise RuntimeError("21220 call not in progress")
                client.created.append((sid, params))
                return type("S", (), {"sid": "MZ123"})()

            def __call__(self, name):
                class One:
                    def update(self, **params):
                        client.updated.append((sid, name, params))
                return One()

        return type("Call", (), {"streams": Streams()})()


async def test_rest_start_uses_the_stream_resource_on_the_prospect_leg():
    client = FakeTwilio()
    sid = await tcs.start_prospect_stream(
        client, backend_endpoint="https://api.example.com", call_ref=CALL_REF,
        leg_call_sid=LEG, secret=SECRET, now=NOW,
    )
    assert sid == "MZ123"
    leg, params = client.created[0]
    assert leg == LEG
    assert params["track"] == "inbound_track"
    assert params["url"] == "wss://api.example.com/api/v1/telephony/dialer-call-sorter"
    assert params["parameter1_name"] == "call_ref" and params["parameter1_value"] == CALL_REF
    assert params["parameter2_name"] == "token"
    assert cs.verify_sorter_token(secret=SECRET, call_ref=CALL_REF, leg_id=LEG, token=params["parameter2_value"], now=NOW)
    assert await tcs.stop_prospect_stream(client, leg_call_sid=LEG)
    assert client.updated == [(LEG, f"sorter-{LEG}", {"status": "stopped"})]


async def test_rest_start_fails_soft(monkeypatch):
    assert await tcs.start_prospect_stream(
        FakeTwilio(fail=True), backend_endpoint="https://api.example.com",
        call_ref=CALL_REF, leg_call_sid=LEG, secret=SECRET,
    ) is None
    monkeypatch.delenv("SYSEVO_DIALER_CALL_SORTER")
    client = FakeTwilio()
    assert await tcs.start_prospect_stream(
        client, backend_endpoint="https://api.example.com",
        call_ref=CALL_REF, leg_call_sid=LEG, secret=SECRET,
    ) is None
    assert client.created == []


# ── Twilio websocket ────────────────────────────────────────────────────────────

class FakeTranscriber:
    """Stands in for LiveTranscriber: emits scripted captions after N frames."""

    script: dict = {}

    def __init__(self, call_id, on_text, *, speaker=None, source=None):
        self.on_text = on_text
        self.enabled = True
        self.frames = 0
        self.speaker = speaker

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def feed(self, frame):
        self.frames += 1
        text = self.script.get(self.frames)
        if text:
            await self.on_text({"type": "transcript", "text": text, "is_final": True})


class FakeTwilioSocket:
    def __init__(self, messages):
        self.incoming = [json.dumps(m) for m in messages]
        self.accepted = False
        self.closed = None

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        if not self.incoming:
            raise RuntimeError("disconnected")
        return self.incoming.pop(0)

    async def close(self, code=1000, reason=""):
        self.closed = code


def _twilio_messages(*, token, call_sid=LEG, call_ref=CALL_REF, audio=b"", track="inbound"):
    messages = [
        {"event": "connected"},
        {"event": "start", "start": {"callSid": call_sid, "customParameters": {"call_ref": call_ref, "token": token}}},
    ]
    for frame in _frames(audio):
        messages.append({"event": "media", "media": {"track": track, "payload": base64.b64encode(frame).decode()}})
    messages.append({"event": "stop"})
    return messages


async def _secret():
    return SECRET


async def test_twilio_stream_with_a_good_token_is_sorted_and_closed():
    rec = Recorder()
    token = cs.issue_sorter_token(secret=SECRET, call_ref=CALL_REF, leg_id=LEG)
    socket = FakeTwilioSocket(_twilio_messages(token=token, audio=_speech(1.0)))
    FakeTranscriber.script = {10: "Acme. This is Dana. How can I help you?"}
    sorter = await tcs.serve_twilio_sorter(
        socket, _secret,
        sorter_factory=lambda ref, provider: cs.CallSorter(ref, emit=rec.emit, publish_state=rec.publish, provider=provider),
        transcriber_factory=FakeTranscriber,
    )
    assert rec.stages == ["person"]
    assert rec.calls[0][2]["provider"] == "twilio"
    assert sorter.done and socket.closed == 1000
    # It stopped reading as soon as it had decided.
    assert any('"media"' in m for m in socket.incoming)


@pytest.mark.parametrize("case", ["wrong_leg", "wrong_ref", "bad_token", "no_start"])
async def test_twilio_stream_is_refused_without_a_valid_token(case):
    token = cs.issue_sorter_token(secret=SECRET, call_ref=CALL_REF, leg_id=LEG)
    if case == "wrong_leg":
        messages = _twilio_messages(token=token, call_sid="CA" + "e" * 32)
    elif case == "wrong_ref":
        messages = _twilio_messages(token=token, call_ref="CA" + "f" * 32)
    elif case == "bad_token":
        messages = _twilio_messages(token="123.abc")
    else:
        messages = [{"event": "connected"}, {"event": "stop"}]
    socket = FakeTwilioSocket(messages)
    made = []
    result = await tcs.serve_twilio_sorter(
        socket, _secret, sorter_factory=lambda *a, **k: made.append(1), transcriber_factory=FakeTranscriber,
    )
    assert result is None and made == []
    assert socket.closed in (4400, 4401)


def test_only_the_inbound_track_is_decoded():
    payload = base64.b64encode(b"\xff" * 160).decode()
    assert tcs.decode_inbound_media({"media": {"track": "inbound", "payload": payload}}) == b"\xff" * 160
    assert tcs.decode_inbound_media({"media": {"track": "outbound", "payload": payload}}) == b""
    assert tcs.decode_inbound_media({"media": {"track": "inbound", "payload": "@@@"}}) == b""


# ── SignalWire ──────────────────────────────────────────────────────────────────

def _query(url):
    from urllib.parse import parse_qs, urlparse

    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_signalwire_tap_url_is_signed_for_the_call_and_leg():
    url = swcs.sorter_tap_url(backend_endpoint="https://api.example.com", call_ref=CALL_REF, leg_id="sw-leg-1")
    assert url.startswith("wss://api.example.com/api/v1/telephony/sw-call-sorter?")
    query = _query(url)
    assert query["call_ref"] == CALL_REF and query["leg"] == "sw-leg-1"
    assert cs.verify_sorter_token(secret="sw-test-token", call_ref=CALL_REF, leg_id="sw-leg-1", token=query["t"])


def test_signalwire_tap_url_is_empty_when_off(monkeypatch):
    monkeypatch.delenv("SYSEVO_DIALER_CALL_SORTER")
    assert swcs.sorter_tap_url(backend_endpoint="https://api.example.com", call_ref=CALL_REF, leg_id="x") == ""


def test_tap_step_on_the_prospect_leg_taps_what_they_say():
    step = swcs.prospect_tap_step("wss://x/y")
    assert step == {"tap": {"uri": "wss://x/y", "direction": "speak", "codec": "PCMU", "control_id": "call-sorter"}}
    assert swcs.prospect_tap_step("wss://x/y", on_prospect_leg=False)["tap"]["direction"] == "listen"
    assert swcs.prospect_tap_step("") is None
    assert swcs.stop_tap_step() == {"stop_tap": {"control_id": "call-sorter"}}


def test_tap_goes_first_on_the_screened_prospect_leg():
    swml = build_signalwire_parallel_leg_swml(decision_url="https://api.example.com/decide")
    tapped = swcs.with_prospect_tap(swml, swcs.prospect_tap_step("wss://x/y"))
    main = tapped["sections"]["main"]
    assert list(main[0]) == ["tap"] and list(main[1]) == ["detect_machine"]
    assert swml["sections"]["main"][0].get("detect_machine")  # original untouched


def test_tap_goes_before_connect_on_the_rep_leg_bridge():
    swml = build_dialer_swml(lead_number="+15550100000", caller_id="+15550100001", recording_webhook="https://r")
    tapped = swcs.with_prospect_tap(swml, swcs.prospect_tap_step("wss://x/y", on_prospect_leg=False), before="connect")
    verbs = [next(iter(step)) for step in tapped["sections"]["main"]]
    assert verbs == ["record_call", "tap", "connect"]
    assert swcs.with_prospect_tap(swml, None) is swml


class FakeSignalWireSocket:
    def __init__(self, query, messages):
        self.query_params = query
        self.messages = list(messages)
        self.accepted = False
        self.closed = None

    async def accept(self):
        self.accepted = True

    async def receive(self):
        if not self.messages:
            return {"type": "websocket.disconnect"}
        return self.messages.pop(0)

    async def close(self, code=1000, reason=""):
        self.closed = code


async def test_signalwire_tap_is_refused_before_the_handshake():
    socket = FakeSignalWireSocket({"call_ref": CALL_REF, "leg": "x", "t": "1.bad"}, [])
    assert await swcs.serve_signalwire_sorter(socket, transcriber_factory=FakeTranscriber) is None
    assert socket.accepted is False and socket.closed == 4401


async def test_signalwire_tap_is_sorted():
    rec = Recorder()
    url = swcs.sorter_tap_url(backend_endpoint="https://api.example.com", call_ref=CALL_REF, leg_id="sw-1")
    audio = _speech(1.0) + _tone(0.4)
    messages = [{"type": "websocket.receive", "text": '{"event":"connected"}'}]
    messages += [{"type": "websocket.receive", "bytes": f} for f in _frames(audio)]
    FakeTranscriber.script = {5: "You've reached Jordan. Please leave a message after the beep."}
    socket = FakeSignalWireSocket(_query(url), messages)
    sorter = await swcs.serve_signalwire_sorter(
        socket,
        sorter_factory=lambda ref, provider: cs.CallSorter(ref, emit=rec.emit, publish_state=rec.publish, provider=provider),
        transcriber_factory=FakeTranscriber,
    )
    assert socket.accepted
    assert rec.stages == ["machine", "beep"]
    assert sorter.done and socket.closed == 1000


def test_signalwire_json_media_frames_are_understood_too():
    payload = base64.b64encode(b"\x7f" * 10).decode()
    assert swcs.decode_tap_message({"text": json.dumps({"event": "media", "media": {"payload": payload}})}) == b"\x7f" * 10
    assert swcs.decode_tap_message({"text": "not json"}) == b""
    assert swcs.decode_tap_message({"bytes": b"\x01\x02"}) == b"\x01\x02"
