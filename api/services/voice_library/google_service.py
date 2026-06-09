"""Google Cloud Text-to-Speech voice service for the voice library."""

import json
from typing import Optional

from loguru import logger

_SSML_GENDER_MAP = {1: "male", 2: "female", 3: "neutral"}


def _make_credentials(credentials_json: Optional[str]):
    from google.oauth2 import service_account

    if not credentials_json:
        return None
    info = json.loads(credentials_json)
    return service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )


async def fetch_google_tts_voices(credentials_json: Optional[str] = None) -> list[dict]:
    """Fetch all available voices from Google Cloud Text-to-Speech API.

    Returns a list of dicts with keys: name, gender, language_codes.
    """
    try:
        from google.cloud import texttospeech_v1 as tts

        creds = _make_credentials(credentials_json)
        client = tts.TextToSpeechAsyncClient(credentials=creds)
        response = await client.list_voices(request={})
        voices = []
        for voice in response.voices:
            voices.append(
                {
                    "name": voice.name,
                    "gender": _SSML_GENDER_MAP.get(int(voice.ssml_gender)),
                    "language_codes": list(voice.language_codes),
                    "natural_sample_rate": voice.natural_sample_rate_hertz,
                }
            )
        await client.transport.close()
        return voices
    except ImportError:
        logger.warning("google-cloud-texttospeech not installed — cannot fetch Google TTS voices")
        return []
    except Exception as e:
        logger.warning(f"Failed to fetch Google TTS voices: {e}")
        raise


async def synthesize_google_tts_preview(
    text: str,
    voice_name: str,
    language_code: str,
    credentials_json: Optional[str] = None,
) -> bytes:
    """Synthesize speech via Google Cloud TTS and return MP3 bytes."""
    from google.cloud import texttospeech_v1 as tts

    creds = _make_credentials(credentials_json)
    client = tts.TextToSpeechAsyncClient(credentials=creds)
    response = await client.synthesize_speech(
        input=tts.SynthesisInput(text=text),
        voice=tts.VoiceSelectionParams(language_code=language_code, name=voice_name),
        audio_config=tts.AudioConfig(audio_encoding=tts.AudioEncoding.MP3),
    )
    await client.transport.close()
    return response.audio_content
