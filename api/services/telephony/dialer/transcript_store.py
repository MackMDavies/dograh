"""Preserve finalized Twilio captions while the full recording is finalized.

Interim hypotheses are never stored. This path cannot interrupt a phone call, and
only the server's signed, attributed Twilio stream supplies these segments.
"""
import hashlib
import math
import re

import httpx
from loguru import logger

from api.constants import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL


async def persist_live_caption(call_sid: str, payload: dict) -> bool:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return False
    if not re.fullmatch(r"CA[0-9a-fA-F]{32}", call_sid or "") or payload.get("is_final") is not True:
        return False
    text = str(payload.get("text") or "").strip()
    speaker = payload.get("speaker")
    if not text or len(text) > 12000 or speaker not in {"rep", "prospect"}:
        return False
    try:
        start = float(payload.get("start"))
        duration = float(payload.get("duration"))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(start) or not math.isfinite(duration) or start < 0 or duration < 0:
        return False
    identity = f"{payload.get('stream_id', '')}:{speaker}:{start:.3f}:{duration:.3f}"
    segment_key = hashlib.sha256(identity.encode()).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/dialer_append_live_transcript",
                headers={"apikey": SUPABASE_SERVICE_ROLE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"},
                json={"p_parent_call_sid": call_sid, "p_segment_key": segment_key, "p_speaker": speaker,
                      "p_start_seconds": start, "p_end_seconds": start + duration, "p_text": text},
            )
            response.raise_for_status()
            return response.json() is True
    except Exception as exc:  # noqa: BLE001 - transcript persistence cannot affect call audio
        logger.warning(f"Final caption persistence failed for {call_sid}: {type(exc).__name__}")
        return False
