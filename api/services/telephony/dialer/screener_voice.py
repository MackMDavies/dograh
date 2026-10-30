"""Speech for the voice-agent style of screener answer (Dialer Phase 2).

Managers pick a voice from the voice library the voice agents use
(``dialer_account_voicemail_settings.screener_agent_voice`` = {provider, voice_id, name}).
How that voice is rendered on the prospect's line:

* ``polly`` / ``amazon`` / ``google`` / ``twilio`` voices are the carrier's own text to
  speech: Twilio ``<Say voice="Polly.Joanna-Neural">`` / ``"Google.en-US-Neural2-F"``.
  No extra API, no stored audio.
* ``elevenlabs`` voices are synthesised once per (voice, text) with the platform key
  (8 kHz mu-law WAV), cached in the private ``voicemail-drops`` bucket under
  ``screener-tts/<account>/<sha>.wav`` and played from a short-lived signed URL. The
  answers are templates, so the same few sentences are reused across calls.
* anything else, or any failure: the carrier's default voice. The answer is still said.

Never raises: the caller always gets something it can play or say.
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

from api.constants import SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL

NATIVE_PROVIDERS = {"polly": "Polly", "amazon": "Polly", "google": "Google", "twilio": ""}
_VOICE_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_ACCOUNT = re.compile(r"^[0-9a-f-]{36}$")


@dataclass(frozen=True)
class Speech:
    """Either a URL to <Play> or text to <Say> (with an optional carrier voice name)."""

    text: str
    url: str | None = None
    say_voice: str | None = None


def carrier_voice(voice: dict[str, Any] | None) -> str | None:
    """Twilio's voice attribute for a library voice it can speak natively, else None."""
    if not isinstance(voice, dict):
        return None
    provider = str(voice.get("provider") or "").strip().lower()
    voice_id = str(voice.get("voice_id") or "").strip()
    if provider not in NATIVE_PROVIDERS or not _VOICE_ID.fullmatch(voice_id):
        return None
    prefix = NATIVE_PROVIDERS[provider]
    if prefix and not voice_id.startswith(prefix + "."):
        return f"{prefix}.{voice_id}"
    return voice_id


def mulaw_wav(audio: bytes, *, sample_rate: int = 8000) -> bytes:
    """Wrap raw 8-bit mu-law samples in a WAV container (format 7)."""
    fmt = struct.pack("<HHIIHH", 7, 1, sample_rate, sample_rate, 1, 8) + struct.pack("<H", 0)
    fact = struct.pack("<I", len(audio))
    data = audio + (b"\x00" if len(audio) % 2 else b"")
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"fact" + struct.pack("<I", 4) + fact + b"data" + struct.pack("<I", len(audio)) + data)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def cache_path(account_id: str, voice: dict[str, Any], text: str) -> str:
    key = f"{voice.get('provider')}|{voice.get('voice_id')}|{text}".encode("utf-8")
    return f"screener-tts/{account_id}/{hashlib.sha256(key).hexdigest()[:40]}.wav"


def _storage_headers(content_type: str | None = None) -> dict[str, str]:
    headers = {"apikey": SUPABASE_ANON_KEY or SUPABASE_SERVICE_ROLE_KEY,
               "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


async def _sign(path: str) -> str | None:
    from api.services.telephony.providers.twilio.dialer_call_log import sign_voicemail_storage_path
    try:
        url = await sign_voicemail_storage_path(path, expires_in=600)
    except Exception:  # noqa: BLE001 - not cached yet / storage down
        return None
    return url if isinstance(url, str) and url.startswith("https://") else None


async def _elevenlabs_audio(voice_id: str, text: str) -> bytes | None:
    from api.services.voice_library.elevenlabs_service import ELEVENLABS_BASE_URL, get_system_elevenlabs_api_key

    key = await get_system_elevenlabs_api_key()
    if not key:
        return None
    async with httpx.AsyncClient(timeout=6.0) as client:
        response = await client.post(
            f"{ELEVENLABS_BASE_URL}/v1/text-to-speech/{voice_id}",
            params={"output_format": "ulaw_8000"},
            json={"text": text, "model_id": "eleven_turbo_v2_5"},
            headers={"xi-api-key": key, "Accept": "audio/basic"},
        )
        response.raise_for_status()
        return response.content or None


async def _cached_elevenlabs(account_id: str, voice: dict[str, Any], text: str) -> str | None:
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY) or not _ACCOUNT.fullmatch(account_id or ""):
        return None
    voice_id = str(voice.get("voice_id") or "")
    if not _VOICE_ID.fullmatch(voice_id):
        return None
    path = cache_path(account_id, voice, text)
    url = await _sign(path)
    if url:
        return url
    audio = await _elevenlabs_audio(voice_id, text)
    if not audio:
        return None
    async with httpx.AsyncClient(timeout=6.0) as client:
        response = await client.post(
            f"{SUPABASE_URL}/storage/v1/object/voicemail-drops/{path}",
            content=mulaw_wav(audio), headers={**_storage_headers("audio/wav"), "x-upsert": "true"},
        )
        response.raise_for_status()
    return await _sign(path)


async def speech_for(text: str, voice: dict[str, Any] | None, *, account_id: str) -> Speech:
    """How to say ``text`` in the account's chosen screener voice."""
    text = " ".join(str(text or "").split())[:600]
    native = carrier_voice(voice)
    if native is not None or not isinstance(voice, dict):
        return Speech(text=text, say_voice=native or None)
    if str(voice.get("provider") or "").lower() == "elevenlabs":
        try:
            url = await _cached_elevenlabs(account_id, voice, text)
            if url:
                return Speech(text=text, url=url)
        except Exception as exc:  # noqa: BLE001 - the carrier voice still says it
            logger.info(f"Screener voice synthesis unavailable: {type(exc).__name__}")
    return Speech(text=text)


__all__ = ["Speech", "cache_path", "carrier_voice", "mulaw_wav", "speech_for"]
