"""Live, speaker-attributed captions for Twilio dialer calls.

SignalWire calls get captions from the SWML tap, which mixes both directions into one
mono stream. Twilio dialer calls had nothing at all, so the live coach was dead on the
main provider. This adds a Twilio Media Stream on the REP's own leg:

    rep leg --<Start><Stream track="both_tracks">--> /dialer-twilio-transcribe
        inbound track  (audio Twilio receives FROM the rep's browser) -> speaker "rep"
        outbound track (audio Twilio sends TO the rep: the conference mix,
                        i.e. the prospect and anyone else on the call) -> speaker "prospect"

Each track feeds its own Deepgram stream, so attribution comes from the carrier's
channel separation, never from a guess about content. Captions are published on the
same Redis text channel /sw-listen already relays, keyed by the provider call id that
dialer_calls.parent_call_sid (outbound) or inbound_calls.provider_call_id (inbound)
holds, so the listener's lookup works unchanged.

Authentication: Twilio Media Streams cannot carry query strings, so the stream carries
<Parameter> values. The token is an HMAC over the channel key, the rep leg's CallSid
and an expiry, keyed by the dialer's Twilio auth token -- a secret that necessarily
exists on any box that can place a Twilio call, so there is no new variable to forget.
The socket also checks that Twilio's `start.callSid` is the leg the token was issued
for, so a leaked token cannot be replayed from another call.

Nothing here may break a call: building the TwiML returns "" whenever anything is
missing, and the socket handler swallows every failure.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from contextlib import AsyncExitStack, suppress
from typing import Awaitable, Callable
from xml.sax.saxutils import quoteattr

from loguru import logger

from api.services.telephony.dialer.live_transcribe import LiveTranscriber
from api.services.telephony.dialer.tap_relay import publish_text

STREAM_PATH = "/api/v1/telephony/dialer-twilio-transcribe"
SOURCE = "twilio_stream"
# The stream starts the moment the TwiML runs, so the token only has to survive the
# round trip from voice-connect to Twilio opening the socket.
TOKEN_TTL_SECONDS = 300
_START_TIMEOUT_SECONDS = 10.0

# Twilio track name -> who is speaking on it, from the REP leg's point of view.
TRACK_SPEAKER = {"inbound": "rep", "outbound": "prospect"}


def live_transcription_enabled() -> bool:
    """Whether to attach the stream at all.

    Needs a Deepgram key (without one the stream would carry audio nowhere useful) and
    honours an explicit kill switch, so a stream problem on real calls can be turned off
    by editing .env and restarting, without a revert.
    """
    if (os.environ.get("SYSEVO_DIALER_LIVE_TRANSCRIBE") or "").strip().lower() in (
        "0", "off", "false", "no",
    ):
        return False
    return bool((os.environ.get("DEEPGRAM_API_KEY") or "").strip())


def _signature(secret: str, channel_key: str, leg_call_sid: str, expires_at: int) -> str:
    message = f"{channel_key}.{leg_call_sid}.{expires_at}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def issue_stream_token(
    *, secret: str, channel_key: str, leg_call_sid: str, now: int | None = None
) -> str:
    """`<expiry>.<hex hmac>`, or "" when it cannot be issued."""
    if not secret or not channel_key or not leg_call_sid:
        return ""
    expires_at = (int(time.time()) if now is None else now) + TOKEN_TTL_SECONDS
    return f"{expires_at}.{_signature(secret, channel_key, leg_call_sid, expires_at)}"


def verify_stream_token(
    *, secret: str, channel_key: str, leg_call_sid: str, token: str, now: int | None = None
) -> bool:
    if not secret or not channel_key or not leg_call_sid or not isinstance(token, str):
        return False
    if token.count(".") != 1:
        return False
    raw_expiry, signature = token.split(".", 1)
    try:
        expires_at = int(raw_expiry)
    except ValueError:
        return False
    current = int(time.time()) if now is None else now
    if expires_at <= current or expires_at > current + TOKEN_TTL_SECONDS:
        return False
    expected = _signature(secret, channel_key, leg_call_sid, expires_at)
    return hmac.compare_digest(signature, expected)


def _stream_url(backend_endpoint: str) -> str:
    base = str(backend_endpoint or "").strip().rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):] + STREAM_PATH
    if base.startswith("wss://"):
        return base + STREAM_PATH
    # Plain http/ws is never used for a live customer conversation.
    return ""


def stream_twiml(
    *, backend_endpoint: str, channel_key: str, leg_call_sid: str, secret: str,
    now: int | None = None,
) -> str:
    """The <Start><Stream> block for the rep's leg, or "" when it should be omitted."""
    if not live_transcription_enabled():
        return ""
    url = _stream_url(backend_endpoint)
    token = issue_stream_token(
        secret=secret, channel_key=channel_key, leg_call_sid=leg_call_sid, now=now
    )
    if not url or not token:
        return ""
    return (
        "<Start>"
        f'<Stream url={quoteattr(url)} track="both_tracks">'
        f'<Parameter name="channel_key" value={quoteattr(channel_key)}/>'
        f'<Parameter name="token" value={quoteattr(token)}/>'
        "</Stream>"
        "</Start>"
    )


def decode_media(message: dict) -> tuple[str | None, bytes]:
    """(speaker, mulaw bytes) for one Twilio `media` frame; (None, b"") when unusable."""
    media = message.get("media") or {}
    speaker = TRACK_SPEAKER.get(str(media.get("track") or "").lower())
    payload = media.get("payload")
    if speaker is None or not isinstance(payload, str) or not payload:
        return None, b""
    try:
        return speaker, base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None, b""


async def _read_start(websocket) -> dict | None:
    """Skip `connected`, return the `start` frame's body, or None."""
    while True:
        raw = await websocket.receive_text()
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            continue
        event = message.get("event")
        if event == "start":
            return message.get("start") or {}
        if event == "stop":
            return None


async def serve_twilio_transcription(
    websocket,
    resolve_secret: Callable[[], Awaitable[str | None]],
    *,
    publish: Callable[[str, dict], Awaitable[None]] = publish_text,
    transcriber_factory=LiveTranscriber,
) -> None:
    """Run one Twilio Media Stream: authenticate, then caption both tracks.

    Twilio only reveals the custom parameters in the `start` frame, so the socket must
    be accepted before it can be checked; an unauthenticated stream is closed straight
    after with 4401 and nothing is published.
    """
    await websocket.accept()
    try:
        start = await asyncio.wait_for(_read_start(websocket), timeout=_START_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - timeout, disconnect, malformed
        start = None
    if not start:
        with suppress(Exception):
            await websocket.close(code=4400, reason="no start")
        return

    params = start.get("customParameters") or {}
    channel_key = str(params.get("channel_key") or "").strip()
    token = str(params.get("token") or "").strip()
    leg_call_sid = str(start.get("callSid") or "").strip()
    secret = ""
    with suppress(Exception):
        secret = (await resolve_secret()) or ""
    if not verify_stream_token(
        secret=secret, channel_key=channel_key, leg_call_sid=leg_call_sid, token=token
    ):
        logger.warning("Rejected an unauthenticated Twilio transcription stream")
        with suppress(Exception):
            await websocket.close(code=4401, reason="not permitted")
        return

    logger.info(f"twilio live transcription attached for {channel_key}")

    async def on_caption(payload: dict) -> None:
        await publish(channel_key, payload)

    try:
        async with AsyncExitStack() as stack:
            transcribers = {}
            # Opened together so neither track's clock starts a connect-time later
            # than the other; their `start` offsets then line up closely enough to
            # interleave the two speakers in order.
            entered = await asyncio.gather(*(
                stack.enter_async_context(
                    transcriber_factory(
                        f"{channel_key}:{speaker}", on_caption, speaker=speaker, source=SOURCE
                    )
                )
                for speaker in ("rep", "prospect")
            ))
            transcribers = dict(zip(("rep", "prospect"), entered))
            if not any(t.enabled for t in transcribers.values()):
                logger.info(f"twilio live transcription unavailable for {channel_key}")
            while True:
                raw = await websocket.receive_text()
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                event = message.get("event")
                if event == "stop":
                    break
                if event != "media":
                    continue
                speaker, audio = decode_media(message)
                transcriber = transcribers.get(speaker) if speaker else None
                if transcriber is not None and audio:
                    await transcriber.feed(audio)
    except Exception as exc:  # noqa: BLE001 - disconnects land here; a live call is never affected
        logger.info(f"twilio live transcription ended for {channel_key}: {type(exc).__name__}")
    logger.info(f"twilio live transcription closed for {channel_key}")


__all__ = [
    "STREAM_PATH",
    "TRACK_SPEAKER",
    "decode_media",
    "issue_stream_token",
    "live_transcription_enabled",
    "serve_twilio_transcription",
    "stream_twiml",
    "verify_stream_token",
]
