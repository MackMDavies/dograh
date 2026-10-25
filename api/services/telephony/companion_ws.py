"""The colleague's media stream on a three-way call (see services/pipecat/companion.py).

    WS /telephony/companion/{transfer_id}

Whichever API worker accepts it: complete the carrier's stream handshake (the same
start messages each provider's handle_websocket reads), build that carrier's
serializer through its own transport factory (so codecs and sample rates match the
caller's call exactly), then relay audio: the colleague's voice to Redis "up", and
whatever arrives on "down" (the caller + the agent) back to the colleague. "end" on
the control channel hangs the socket up; the socket closing tells the call "left".
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from api.services.pipecat.companion import control_channel, down_channel, up_channel

router = APIRouter()


async def _handshake(provider: str, websocket: WebSocket, leg_id: Optional[str]) -> Optional[dict[str, Any]]:
    """The transport_kwargs each provider's transport factory needs."""
    if provider in ("twilio", "cloudonix"):
        if json.loads(await websocket.receive_text()).get("event") != "connected":
            return None
        start = json.loads(await websocket.receive_text())
        if start.get("event") != "start":
            return None
        info = start.get("start") or {}
        if provider == "twilio":
            return {"stream_sid": info.get("streamSid"), "call_sid": info.get("callSid")}
        return {"stream_sid": info.get("streamSid"), "call_id": leg_id or info.get("callSid")}
    if provider == "telnyx":
        if json.loads(await websocket.receive_text()).get("event") != "connected":
            return None
        start = json.loads(await websocket.receive_text())
        info = start.get("start") or {}
        return {
            "stream_id": start.get("stream_id", ""),
            "call_control_id": info.get("call_control_id") or leg_id,
            "encoding": (info.get("media_format") or {}).get("encoding") or "PCMU",
        }
    if provider in ("plivo", "vobiz"):
        start = json.loads(await websocket.receive_text())
        if start.get("event") != "start":
            return None
        info = start.get("start") or {}
        return {
            "stream_id": info.get("streamId") or start.get("streamId"),
            "call_id": info.get("callId") or info.get("callUUID") or leg_id,
        }
    if provider == "vonage":
        return {"call_uuid": leg_id}
    return None


@router.websocket("/companion/{transfer_id}")
async def companion_socket(websocket: WebSocket, transfer_id: str):
    import redis.asyncio as aioredis

    from api.constants import REDIS_URL
    from api.db import db_client
    from api.services.pipecat.audio_config import create_audio_config
    from api.services.telephony import registry as telephony_registry
    from api.services.telephony.call_transfer_manager import get_call_transfer_manager
    from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, StartFrame

    await websocket.accept()
    context = await (await get_call_transfer_manager()).get_transfer_context(transfer_id)
    run = await db_client.get_workflow_run_by_id(int(context.workflow_run_id)) if context and context.workflow_run_id else None
    if context is None or not context.three_way or run is None:
        await websocket.close(code=4404, reason="Unknown three-way call")
        return
    provider = str(run.mode)
    workflow = await db_client.get_workflow_by_id(run.workflow_id)
    try:
        transport_kwargs = await _handshake(provider, websocket, context.call_sid)
    except (WebSocketDisconnect, ValueError) as e:
        logger.info(f"[companion {transfer_id}] closed during the handshake: {e}")
        return
    if not transport_kwargs or not workflow:
        await websocket.close(code=4400, reason="Unexpected stream start")
        return

    audio_config = create_audio_config(provider)
    transport = await telephony_registry.get(provider).transport_factory(
        websocket,
        run.id,
        audio_config,
        workflow.organization_id,
        telephony_configuration_id=(run.initial_context or {}).get("telephony_configuration_id"),
        **transport_kwargs,
    )
    serializer = transport._params.serializer
    with suppress(AttributeError):
        serializer._params.auto_hang_up = False  # the call's own pipeline decides that
    rate_in = audio_config.transport_in_sample_rate
    rate_out = audio_config.transport_out_sample_rate
    await serializer.setup(StartFrame(audio_in_sample_rate=rate_in, audio_out_sample_rate=rate_out))

    redis = aioredis.from_url(REDIS_URL)
    pubsub = redis.pubsub()
    await pubsub.subscribe(down_channel(transfer_id), control_channel(transfer_id))
    logger.info(f"[companion {transfer_id}] the colleague's audio is connected ({provider})")

    async def colleague_to_call() -> None:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            data = message.get("text") if message.get("text") is not None else message.get("bytes")
            if data is None:
                continue
            frame = await serializer.deserialize(data)
            if isinstance(frame, InputAudioRawFrame) and frame.audio:
                await redis.publish(up_channel(transfer_id), frame.audio)

    async def call_to_colleague() -> None:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            channel = message["channel"]
            channel = channel.decode() if isinstance(channel, bytes) else channel
            if channel.endswith(":ctl"):
                if message["data"] in (b"end", "end"):
                    return
                continue
            payload = await serializer.serialize(
                OutputAudioRawFrame(audio=message["data"], sample_rate=rate_out, num_channels=1)
            )
            if isinstance(payload, (bytes, bytearray)):
                await websocket.send_bytes(bytes(payload))
            elif payload:
                await websocket.send_text(payload)

    tasks = [asyncio.create_task(colleague_to_call()), asyncio.create_task(call_to_colleague())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                logger.warning(f"[companion {transfer_id}] relay stopped: {task.exception()!r}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[companion {transfer_id}] relay stopped: {e}")
    finally:
        for task in tasks:
            task.cancel()
        with suppress(Exception):
            await redis.publish(control_channel(transfer_id), b"left")
        with suppress(Exception):
            await pubsub.aclose()
        with suppress(Exception):
            await redis.aclose()
        with suppress(Exception):
            await websocket.close()
        logger.info(f"[companion {transfer_id}] the colleague's audio is closed")
