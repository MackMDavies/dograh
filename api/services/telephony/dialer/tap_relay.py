"""Live call audio, forked to whoever is allowed to hear it.

SWML's `tap` verb streams a live call's audio to a WebSocket while the call carries on
untouched. That is the whole reason this exists rather than a conference: monitoring by
joining a conference would mean re-architecting the outbound path so the lead is
originated into a room instead of bridged with `connect`, and that path took two outages
to get working. A tap is additive, like record_call.

    call --tap--> /sw-tap --> redis --> /sw-listen --> manager's browser

WHY REDIS AND NOT A DICT. The API runs multiple uvicorn workers. SignalWire's tap lands on
whichever worker the load balancer picks, and the manager's browser lands on another one;
an in-process registry would work perfectly in development and silently relay nothing in
production roughly half the time. Audio here is 8 kHz mu-law, so a call is about 8 KB/s --
a rounding error for Redis, and the correct trade for never having to debug that.

Nothing is persisted. This is a live relay: a manager who joins halfway through hears the
call from the moment they joined, and the recording remains the record of what was said.
"""

import asyncio
import json
import os
from contextlib import suppress
from typing import AsyncIterator, Awaitable, Callable

import redis.asyncio as aioredis
from loguru import logger

_CHANNEL_PREFIX = "dialer:tap:"
_TEXT_SUFFIX = ":text"

# A frame is 20ms of mu-law by default, so this is about four seconds of audio. Enough to
# ride out a slow consumer, small enough that a listener who stalls gets dropped rather
# than accumulating a growing delay behind the live call.
_LISTENER_QUEUE_FRAMES = 200


def channel_for(call_id: str) -> str:
    return f"{_CHANNEL_PREFIX}{call_id}"


def text_channel_for(call_id: str) -> str:
    """Live captions, on their own channel.

    Separate from the audio rather than multiplexed onto it: raw mu-law and JSON are both
    just bytes, and telling them apart by inspecting the payload is the kind of guess that
    turns a transcript into a burst of noise in somebody's ear.
    """
    return f"{_CHANNEL_PREFIX}{call_id}{_TEXT_SUFFIX}"


def _redis_url() -> str:
    return os.environ.get("REDIS_URL") or "redis://redis:6379"


async def publish_text(call_id: str, payload: dict) -> None:
    """Publish one caption. Never raises: captions are the least important thing here."""
    client = None
    try:
        client = aioredis.from_url(_redis_url())
        await client.publish(text_channel_for(call_id), json.dumps(payload))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"caption publish failed for {call_id}: {exc}")
    finally:
        if client is not None:
            with suppress(Exception):
                await client.aclose()


async def publish_frames(
    call_id: str,
    frames: AsyncIterator[bytes],
    sink: "Callable[[bytes], Awaitable[None]] | None" = None,
) -> int:
    """Fan a tap's audio out to any listeners. Returns how many frames were relayed.

    `sink` gets every frame too, which is how live transcription rides along on the same
    stream rather than opening a second tap. It must never raise; a transcription problem
    is not allowed to stop the relay.

    Never raises into the caller. This runs while a real call is up, and a Redis problem
    must cost the monitoring feature, never the call.
    """
    channel = channel_for(call_id)
    published = 0
    client = None
    try:
        client = aioredis.from_url(_redis_url())
        async for frame in frames:
            if not frame:
                continue
            try:
                await client.publish(channel, frame)
                published += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"tap relay publish failed for {call_id}: {exc}")
                break
            if sink is not None:
                # Deliberately after the publish and deliberately swallowed: listening is
                # the feature that must survive, captions are the one that may not.
                try:
                    await sink(frame)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"tap sink failed for {call_id}: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"tap relay for {call_id} ended early: {exc}")
    finally:
        if client is not None:
            with suppress(Exception):
                await client.aclose()
    return published


async def subscribe_stream(
    call_id: str, stop: asyncio.Event
) -> AsyncIterator[tuple[str, bytes]]:
    """Yield ("audio", frame) and ("text", json) for a call until `stop` is set.

    Subscribes BEFORE the first yield so a listener cannot miss anything published
    between being told the call is live and actually attaching.
    """
    audio_channel = channel_for(call_id)
    text_channel = text_channel_for(call_id)
    client = aioredis.from_url(_redis_url())
    pubsub = client.pubsub()
    try:
        await pubsub.subscribe(audio_channel, text_channel)
        while not stop.is_set():
            try:
                message = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                # No audio for a moment is normal -- silence is not sent as frames by
                # every provider. Loop so the stop event is still checked promptly.
                continue
            if message is None:
                continue
            data = message.get("data")
            if not isinstance(data, (bytes, bytearray)) or not data:
                continue
            raw_channel = message.get("channel")
            channel_name = (
                raw_channel.decode() if isinstance(raw_channel, (bytes, bytearray)) else str(raw_channel)
            )
            yield ("text" if channel_name.endswith(_TEXT_SUFFIX) else "audio", bytes(data))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"tap listener for {call_id} ended: {exc}")
    finally:
        with suppress(Exception):
            await pubsub.unsubscribe(audio_channel, text_channel)
        with suppress(Exception):
            await pubsub.aclose()
        with suppress(Exception):
            await client.aclose()


__all__ = [
    "channel_for",
    "text_channel_for",
    "publish_frames",
    "publish_text",
    "subscribe_stream",
]
