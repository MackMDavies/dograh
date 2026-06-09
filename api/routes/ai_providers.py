"""API routes for org-level AI provider connection management."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from api.db import db_client
from api.schemas.ai_providers import (
    ApplyMarginSchema,
    AvailableModelResponseSchema,
    CreateProviderConnectionSchema,
    ProviderConnectionResponseSchema,
    SetModelClientAvailableSchema,
    SetModelDisplayNameSchema,
    SetModelOurPriceSchema,
    UpdateProviderConnectionSchema,
)
from api.services.auth.depends import get_user

router = APIRouter(prefix="/ai-providers", tags=["ai-providers"])

# ─── Pricing catalog ──────────────────────────────────────────────────────────
# cost_per_min_usd: estimated cost for 1 min of voice AI usage
# native_cost_display: human-readable provider pricing unit
#
# LLM assumption: ~500 input + 200 output tokens/min of conversation
# TTS assumption: ~500 characters/min of generated speech
# STT: direct per-minute rate from provider
# Realtime: per-minute rate

_PRICING: dict[str, tuple[float, str]] = {
    # ── OpenAI LLM ──────────────────────────────────────────────────────────
    "gpt-4o":                       (0.0033,  "$2.50/M in · $10.00/M out tokens"),
    "gpt-4o-2024-11-20":            (0.0033,  "$2.50/M in · $10.00/M out tokens"),
    "gpt-4o-2024-08-06":            (0.0033,  "$2.50/M in · $10.00/M out tokens"),
    "gpt-4o-2024-05-13":            (0.0050,  "$5.00/M in · $15.00/M out tokens"),
    "gpt-4o-mini":                  (0.00020, "$0.15/M in · $0.60/M out tokens"),
    "gpt-4o-mini-2024-07-18":       (0.00020, "$0.15/M in · $0.60/M out tokens"),
    "gpt-4-turbo":                  (0.0110,  "$10.00/M in · $30.00/M out tokens"),
    "gpt-4-turbo-2024-04-09":       (0.0110,  "$10.00/M in · $30.00/M out tokens"),
    "gpt-4":                        (0.0270,  "$30.00/M in · $60.00/M out tokens"),
    "gpt-4-0613":                   (0.0270,  "$30.00/M in · $60.00/M out tokens"),
    "gpt-3.5-turbo":                (0.00055, "$0.50/M in · $1.50/M out tokens"),
    "gpt-3.5-turbo-0125":           (0.00055, "$0.50/M in · $1.50/M out tokens"),
    "o1":                           (0.0195,  "$15.00/M in · $60.00/M out tokens"),
    "o1-2024-12-17":                (0.0195,  "$15.00/M in · $60.00/M out tokens"),
    "o1-mini":                      (0.0039,  "$3.00/M in · $12.00/M out tokens"),
    "o1-mini-2024-09-12":           (0.0039,  "$3.00/M in · $12.00/M out tokens"),
    "o1-preview":                   (0.0225,  "$15.00/M in · $60.00/M out tokens"),
    "o3":                           (0.0330,  "$10.00/M in · $40.00/M out tokens"),
    "o3-mini":                      (0.0015,  "$1.10/M in · $4.40/M out tokens"),
    "o4-mini":                      (0.0015,  "$1.10/M in · $4.40/M out tokens"),
    # ── OpenAI TTS ──────────────────────────────────────────────────────────
    "tts-1":                        (0.0075,  "$15.00/M characters"),
    "tts-1-hd":                     (0.0150,  "$30.00/M characters"),
    "tts-1-1106":                   (0.0075,  "$15.00/M characters"),
    "tts-1-hd-1106":                (0.0150,  "$30.00/M characters"),
    # ── OpenAI STT ──────────────────────────────────────────────────────────
    "whisper-1":                    (0.0060,  "$0.006/min audio"),
    # ── OpenAI Realtime ─────────────────────────────────────────────────────
    "gpt-4o-realtime-preview":      (0.0600,  "$0.06/min audio in · $0.24/min out"),
    "gpt-4o-realtime-preview-2024-12-17": (0.0600, "$0.06/min audio in · $0.24/min out"),
    "gpt-4o-mini-realtime-preview": (0.0100,  "$0.01/min audio in · $0.02/min out"),
    # ── Anthropic ───────────────────────────────────────────────────────────
    "claude-haiku-4-5":                   (0.0012, "$0.80/M in · $4.00/M out tokens"),
    "claude-haiku-4-5-20251001":          (0.0012, "$0.80/M in · $4.00/M out tokens"),
    "claude-haiku-3-5":                   (0.0012, "$0.80/M in · $4.00/M out tokens"),
    "claude-haiku-3-5-20241022":          (0.0012, "$0.80/M in · $4.00/M out tokens"),
    "claude-sonnet-4-6":                  (0.0045, "$3.00/M in · $15.00/M out tokens"),
    "claude-sonnet-4-5":                  (0.0045, "$3.00/M in · $15.00/M out tokens"),
    "claude-sonnet-4-5-20250929":         (0.0045, "$3.00/M in · $15.00/M out tokens"),
    "claude-sonnet-3-7":                  (0.0045, "$3.00/M in · $15.00/M out tokens"),
    "claude-sonnet-3-7-20250219":         (0.0045, "$3.00/M in · $15.00/M out tokens"),
    "claude-sonnet-3-5":                  (0.0045, "$3.00/M in · $15.00/M out tokens"),
    "claude-sonnet-3-5-20241022":         (0.0045, "$3.00/M in · $15.00/M out tokens"),
    "claude-opus-4-1":                    (0.0225, "$15.00/M in · $75.00/M out tokens"),
    "claude-opus-4-1-20250805":           (0.0225, "$15.00/M in · $75.00/M out tokens"),
    "claude-opus-4-5":                    (0.0225, "$15.00/M in · $75.00/M out tokens"),
    "claude-opus-4-5-20251101":           (0.0225, "$15.00/M in · $75.00/M out tokens"),
    "claude-opus-4-6":                    (0.0225, "$15.00/M in · $75.00/M out tokens"),
    "claude-opus-4-7":                    (0.0225, "$15.00/M in · $75.00/M out tokens"),
    "claude-opus-4-8":                    (0.0225, "$15.00/M in · $75.00/M out tokens"),
    "claude-3-opus-20240229":             (0.0225, "$15.00/M in · $75.00/M out tokens"),
    "claude-3-5-sonnet-20241022":         (0.0045, "$3.00/M in · $15.00/M out tokens"),
    "claude-3-5-haiku-20241022":          (0.0012, "$0.80/M in · $4.00/M out tokens"),
    # ── Google Gemini ────────────────────────────────────────────────────────
    "gemini-2.5-pro":               (0.00219, "$1.25/M in · $10.00/M out tokens"),
    "gemini-2.5-pro-preview-05-06": (0.00219, "$1.25/M in · $10.00/M out tokens"),
    "gemini-2.5-flash":             (0.00023, "$0.15/M in · $0.60/M out tokens"),
    "gemini-2.5-flash-preview-04-17": (0.00023, "$0.15/M in · $0.60/M out tokens"),
    "gemini-2.0-flash":             (0.00010, "$0.075/M in · $0.30/M out tokens"),
    "gemini-2.0-flash-001":         (0.00010, "$0.075/M in · $0.30/M out tokens"),
    "gemini-2.0-flash-lite":        (0.000044, "$0.0375/M in · $0.15/M out tokens"),
    "gemini-2.0-flash-lite-001":    (0.000044, "$0.0375/M in · $0.15/M out tokens"),
    "gemini-2.0-flash-lite-preview-02-05": (0.000044, "$0.0375/M in · $0.15/M out tokens"),
    "gemini-1.5-pro":               (0.00163, "$1.25/M in · $5.00/M out tokens"),
    "gemini-1.5-pro-001":           (0.00163, "$1.25/M in · $5.00/M out tokens"),
    "gemini-1.5-pro-002":           (0.00163, "$1.25/M in · $5.00/M out tokens"),
    "gemini-1.5-flash":             (0.00010, "$0.075/M in · $0.30/M out tokens"),
    "gemini-1.5-flash-001":         (0.00010, "$0.075/M in · $0.30/M out tokens"),
    "gemini-1.5-flash-002":         (0.00010, "$0.075/M in · $0.30/M out tokens"),
    "gemini-1.5-flash-8b":          (0.000056, "$0.0375/M in · $0.15/M out tokens"),
    "gemini-1.5-flash-8b-001":      (0.000056, "$0.0375/M in · $0.15/M out tokens"),
    "deep-research-max-preview-04-2026": (0.0500, "$7.00/M in · $35.00/M out tokens"),
    "deep-research-preview-04-2026":     (0.0300, "$3.50/M in · $17.50/M out tokens"),
    "deep-research-pro-preview-12-2025": (0.0300, "$3.50/M in · $17.50/M out tokens"),
    "antigravity-preview-05-2026":       (0.0219, "$1.25/M in · $10.00/M out tokens"),
    # ── Google TTS ──────────────────────────────────────────────────────────
    "google-tts-standard":          (0.0020,  "$4.00/M characters"),
    "google-tts-wavenet":           (0.0080,  "$16.00/M characters"),
    "google-tts-neural2":           (0.0080,  "$16.00/M characters"),
    "google-tts-studio":            (0.0320,  "$64.00/M characters"),
    # ── Google STT ──────────────────────────────────────────────────────────
    "google-stt-standard":          (0.0040,  "$0.004/min audio"),
    "google-stt-enhanced":          (0.0090,  "$0.009/min audio"),
    "google-stt-chirp":             (0.0160,  "$0.016/min audio"),
    # ── Google Realtime ─────────────────────────────────────────────────────
    "gemini-2.0-flash-exp":         (0.0200,  "Experimental — est. $0.02/min"),
    "gemini-2.0-flash-live-001":    (0.0150,  "$0.015/min audio stream"),
    # ── Groq ────────────────────────────────────────────────────────────────
    "llama3-8b-8192":               (0.000041, "$0.05/M in · $0.08/M out tokens"),
    "llama3-70b-8192":              (0.000453, "$0.59/M in · $0.79/M out tokens"),
    "llama-3.1-8b-instant":         (0.000041, "$0.05/M in · $0.08/M out tokens"),
    "llama-3.1-70b-versatile":      (0.000453, "$0.59/M in · $0.79/M out tokens"),
    "llama-3.3-70b-versatile":      (0.000453, "$0.59/M in · $0.79/M out tokens"),
    "llama-3.3-70b-specdec":        (0.000453, "$0.59/M in · $0.79/M out tokens"),
    "llama-3.2-1b-preview":         (0.000008, "$0.04/M in · $0.04/M out tokens"),
    "llama-3.2-3b-preview":         (0.000030, "$0.06/M in · $0.06/M out tokens"),
    "llama-3.2-11b-vision-preview": (0.000059, "$0.18/M in · $0.18/M out tokens"),
    "llama-3.2-90b-vision-preview": (0.000476, "$0.90/M in · $0.90/M out tokens"),
    "mixtral-8x7b-32768":           (0.000168, "$0.24/M in · $0.24/M out tokens"),
    "gemma-7b-it":                  (0.000035, "$0.07/M in · $0.07/M out tokens"),
    "gemma2-9b-it":                 (0.000100, "$0.20/M in · $0.20/M out tokens"),
    "llama-guard-3-8b":             (0.000041, "$0.20/M in · $0.20/M out tokens"),
    "whisper-large-v3":             (0.0001,   "$0.111/hr audio"),
    "whisper-large-v3-turbo":       (0.0001,   "$0.04/hr audio"),
    "distil-whisper-large-v3-en":   (0.0001,   "$0.02/hr audio"),
    # ── xAI / Grok ──────────────────────────────────────────────────────────
    "grok-2":                       (0.0030,  "$2.00/M in · $10.00/M out tokens"),
    "grok-2-latest":                (0.0030,  "$2.00/M in · $10.00/M out tokens"),
    "grok-2-1212":                  (0.0030,  "$2.00/M in · $10.00/M out tokens"),
    "grok-3":                       (0.0045,  "$3.00/M in · $15.00/M out tokens"),
    "grok-3-latest":                (0.0045,  "$3.00/M in · $15.00/M out tokens"),
    "grok-3-fast":                  (0.0015,  "$5.00/M in · $25.00/M out tokens"),
    "grok-3-mini":                  (0.00040, "$0.30/M in · $0.50/M out tokens"),
    "grok-beta":                    (0.0075,  "$5.00/M in · $15.00/M out tokens"),
    "grok-vision-beta":             (0.0075,  "$5.00/M in · $15.00/M out tokens"),
    # ── xAI TTS ─────────────────────────────────────────────────────────────
    "eve":                          (0.0030,  "xAI TTS — est. $0.006/min"),
    "ara":                          (0.0030,  "xAI TTS — est. $0.006/min"),
    "rex":                          (0.0030,  "xAI TTS — est. $0.006/min"),
    "sal":                          (0.0030,  "xAI TTS — est. $0.006/min"),
    "leo":                          (0.0030,  "xAI TTS — est. $0.006/min"),
    # ── ElevenLabs TTS ──────────────────────────────────────────────────────
    "eleven_flash_v2_5":            (0.000040, "$0.08/M characters"),
    "eleven_flash_v2":              (0.000040, "$0.08/M characters"),
    "eleven_turbo_v2_5":            (0.000040, "$0.08/M characters"),
    "eleven_turbo_v2":              (0.000040, "$0.08/M characters"),
    "eleven_multilingual_v2":       (0.000090, "$0.18/M characters"),
    "eleven_multilingual_v1":       (0.000090, "$0.18/M characters"),
    "eleven_monolingual_v1":        (0.000090, "$0.18/M characters"),
    # ── Deepgram STT ────────────────────────────────────────────────────────
    "nova-2":                       (0.0043,  "$0.0043/min audio"),
    "nova-2-general":               (0.0043,  "$0.0043/min audio"),
    "nova-2-meeting":               (0.0059,  "$0.0059/min audio"),
    "nova-2-phonecall":             (0.0059,  "$0.0059/min audio"),
    "nova-2-medical":               (0.0100,  "$0.0100/min audio"),
    "nova-2-conversationalai":      (0.0043,  "$0.0043/min audio"),
    "nova-2-voicemail":             (0.0043,  "$0.0043/min audio"),
    "nova-2-finance":               (0.0059,  "$0.0059/min audio"),
    "nova-2-video":                 (0.0043,  "$0.0043/min audio"),
    "nova-3":                       (0.0059,  "$0.0059/min audio"),
    "nova-3-general":               (0.0059,  "$0.0059/min audio"),
    "nova":                         (0.0059,  "$0.0059/min audio"),
    "enhanced":                     (0.0145,  "$0.0145/min audio"),
    "base":                         (0.0125,  "$0.0125/min audio"),
    "whisper":                      (0.0200,  "$0.0200/min audio"),
    # ── Deepgram TTS (Aura) ──────────────────────────────────────────────────
    "aura-asteria-en":              (0.0075,  "$15.00/M characters"),
    "aura-luna-en":                 (0.0075,  "$15.00/M characters"),
    "aura-stella-en":               (0.0075,  "$15.00/M characters"),
    "aura-athena-en":               (0.0075,  "$15.00/M characters"),
    "aura-hera-en":                 (0.0075,  "$15.00/M characters"),
    "aura-orion-en":                (0.0075,  "$15.00/M characters"),
    "aura-arcas-en":                (0.0075,  "$15.00/M characters"),
    "aura-perseus-en":              (0.0075,  "$15.00/M characters"),
    "aura-angus-en":                (0.0075,  "$15.00/M characters"),
    "aura-orpheus-en":              (0.0075,  "$15.00/M characters"),
    "aura-helios-en":               (0.0075,  "$15.00/M characters"),
    "aura-zeus-en":                 (0.0075,  "$15.00/M characters"),
    "aura-2-thalia-en":             (0.0060,  "$12.00/M characters"),
    "aura-2-andromeda-en":          (0.0060,  "$12.00/M characters"),
    # ── Cartesia TTS ─────────────────────────────────────────────────────────
    "sonic":                        (0.000045, "$0.09/M characters"),
    "sonic-2":                      (0.000045, "$0.09/M characters"),
    "sonic-english":                (0.000045, "$0.09/M characters"),
    "sonic-multilingual":           (0.000070, "$0.14/M characters"),
    "sonic-preview":                (0.000045, "$0.09/M characters"),
    # ── AssemblyAI STT ───────────────────────────────────────────────────────
    "best":                         (0.0065,  "$0.0065/min audio"),
    "nano":                         (0.0020,  "$0.0020/min audio"),
    "slam-1":                       (0.0065,  "$0.0065/min audio"),
    # ── Gladia STT ───────────────────────────────────────────────────────────
    "gladia-v2":                    (0.0068,  "$0.0068/min audio"),
    "fast":                         (0.0036,  "$0.0036/min audio"),
    "accurate":                     (0.0068,  "$0.0068/min audio"),
    # ── Speechmatics STT ─────────────────────────────────────────────────────
    "speechmatics-enhanced":        (0.0120,  "$0.0120/min audio"),
    "speechmatics-standard":        (0.0060,  "$0.0060/min audio"),
    # ── Rime TTS ─────────────────────────────────────────────────────────────
    "arcas":                        (0.000050, "$0.10/M characters"),
    "mist":                         (0.000050, "$0.10/M characters"),
    # ── Azure OpenAI LLM ─────────────────────────────────────────────────────
    "gpt-4o-azure":                 (0.0033,  "$2.50/M in · $10.00/M out tokens"),
    "gpt-4o-mini-azure":            (0.00020, "$0.15/M in · $0.60/M out tokens"),
    # ── AWS Bedrock ──────────────────────────────────────────────────────────
    "anthropic.claude-3-haiku-20240307-v1:0":  (0.0012, "$0.80/M in · $4.00/M out tokens"),
    "anthropic.claude-3-sonnet-20240229-v1:0": (0.0045, "$3.00/M in · $15.00/M out tokens"),
    "anthropic.claude-3-opus-20240229-v1:0":   (0.0225, "$15.00/M in · $75.00/M out tokens"),
    "amazon.titan-text-lite-v1":               (0.00019, "$0.15/M in · $0.20/M out tokens"),
    "amazon.titan-text-express-v1":            (0.00063, "$0.20/M in · $0.65/M out tokens"),
    "meta.llama3-8b-instruct-v1:0":            (0.000041, "$0.30/M in · $0.60/M out tokens"),
    "meta.llama3-70b-instruct-v1:0":           (0.000453, "$2.65/M in · $3.50/M out tokens"),
    # ── MiniMax ──────────────────────────────────────────────────────────────
    "abab6.5-chat":                 (0.00080, "$0.80/M tokens"),
    "abab6.5s-chat":                (0.00080, "$0.80/M tokens"),
    "abab5.5-chat":                 (0.00015, "$0.15/M tokens"),
    "speech-01-turbo":              (0.000050, "$0.10/M characters"),
    "speech-01-hd":                 (0.000100, "$0.20/M characters"),
    # ── Sarvam STT ───────────────────────────────────────────────────────────
    "saarika:v2":                   (0.0040,  "$0.004/min audio"),
    "saaras:v2":                    (0.0040,  "$0.004/min audio"),
    # ── OpenRouter (pass-through — varies by model) ───────────────────────────
    # These are approximations; actual cost depends on which underlying model is used
    "openai/gpt-4o":                (0.0033,  "via OpenRouter · $2.50/M in · $10.00/M out"),
    "openai/gpt-4o-mini":           (0.00020, "via OpenRouter · $0.15/M in · $0.60/M out"),
    "anthropic/claude-3.5-sonnet":  (0.0045,  "via OpenRouter · $3.00/M in · $15.00/M out"),
    "google/gemini-2.0-flash":      (0.00010, "via OpenRouter · $0.075/M in · $0.30/M out"),
    "meta-llama/llama-3.3-70b-instruct": (0.000453, "via OpenRouter · $0.59/M in · $0.79/M out"),
    # ── Speaches (self-hosted / free) ─────────────────────────────────────────
    "speaches-tts":                 (0.0000,  "Self-hosted · no per-unit cost"),
    "speaches-stt":                 (0.0000,  "Self-hosted · no per-unit cost"),
}


def _get_pricing(model_id: str) -> tuple[float, str] | None:
    """Return (cost_per_min_usd, native_cost_display) for a model, or None if unknown.

    Tries exact match first, then prefix matching for versioned model IDs.
    """
    if model_id in _PRICING:
        return _PRICING[model_id]
    # Prefix/substring match — e.g. "eleven_flash_v2_5" voice IDs for ElevenLabs,
    # or Deepgram voice model variants like "aura-asteria-en-custom"
    for key, val in _PRICING.items():
        if model_id.startswith(key) or key.startswith(model_id):
            return val
    return None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _mask_key(key: Optional[str]) -> Optional[str]:
    if not key or len(key) < 4:
        return key
    return "****" + key[-4:]


def _conn_to_schema(conn, model_count: int = 0) -> ProviderConnectionResponseSchema:
    return ProviderConnectionResponseSchema(
        id=conn.id,
        organization_id=conn.organization_id,
        service_type=conn.service_type,
        provider=conn.provider,
        display_name=conn.display_name,
        api_key_masked=_mask_key(conn.api_key),
        extra_config=conn.extra_config,
        is_active=conn.is_active,
        created_at=conn.created_at,
        updated_at=conn.updated_at,
        model_count=model_count,
    )


def _model_to_schema(m, provider: str) -> AvailableModelResponseSchema:
    return AvailableModelResponseSchema(
        id=m.id,
        connection_id=m.connection_id,
        organization_id=m.organization_id,
        service_type=m.service_type,
        provider=provider,
        model_id=m.model_id,
        display_name=m.display_name,
        is_client_available=m.is_client_available,
        is_default=m.is_default,
        cost_per_min_usd=m.cost_per_min_usd,
        native_cost_display=m.native_cost_display,
        our_price_per_min_usd=m.our_price_per_min_usd,
    )


# ─── Admin: Connections ───────────────────────────────────────────────────────

@router.get("/connections", response_model=list[ProviderConnectionResponseSchema])
async def list_connections(user=Depends(get_user)):
    """List active provider connections. Superusers see all orgs; others see their own."""
    if user.is_superuser:
        conns = await db_client.list_all_connections_superuser()
        all_models = await db_client.list_all_available_models_superuser()
    else:
        conns = await db_client.list_connections(user.selected_organization_id)
        all_models = await db_client.list_available_models(user.selected_organization_id)
    count_by_conn: dict[int, int] = {}
    for m in all_models:
        count_by_conn[m.connection_id] = count_by_conn.get(m.connection_id, 0) + 1
    return [_conn_to_schema(c, count_by_conn.get(c.id, 0)) for c in conns]


_XAI_TTS_VOICES = ["eve", "ara", "rex", "sal", "leo"]


@router.post("/connections", response_model=ProviderConnectionResponseSchema)
async def create_connection(
    body: CreateProviderConnectionSchema,
    user=Depends(get_user),
):
    """Connect a new API provider to the org. Admin only."""
    try:
        conn = await db_client.create_connection(
            organization_id=user.selected_organization_id,
            created_by=user.id,
            service_type=body.service_type,
            provider=body.provider,
            api_key=body.api_key,
            extra_config=body.extra_config,
            display_name=body.display_name,
        )

        # Seed predefined xAI TTS voices into the voice library
        if body.service_type == "tts" and body.provider == "xai":
            org_id = user.selected_organization_id
            for voice_name in _XAI_TTS_VOICES:
                existing = await db_client.get_voice_by_provider_id(voice_name, org_id)
                if existing:
                    logger.info(f"xAI voice '{voice_name}' already exists for org {org_id}, skipping")
                    continue
                await db_client.create_voice(
                    user_id=user.id,
                    organization_id=org_id,
                    name=voice_name.capitalize(),
                    provider="xai",
                    provider_voice_id=voice_name,
                    is_public=True,
                    status="ready",
                    language="en",
                    labels={"provider": "xai"},
                )
                logger.info(f"Seeded xAI voice '{voice_name}' for org {org_id}")

        return _conn_to_schema(conn)
    except Exception as exc:
        logger.error(f"Error creating provider connection: {exc}")
        raise HTTPException(status_code=500, detail="Failed to create connection") from exc


@router.post("/connections/{connection_id}/sync-voices")
async def sync_connection_voices(connection_id: int, user=Depends(get_user)):
    """Seed predefined voices into the voice library for an existing TTS connection."""
    conn = await db_client.get_connection(connection_id, user.selected_organization_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    if conn.service_type != "tts":
        raise HTTPException(status_code=422, detail="Only TTS connections have predefined voices")

    provider_voices: dict[str, list[str]] = {
        "xai": _XAI_TTS_VOICES,
    }
    voice_names = provider_voices.get(conn.provider, [])
    if not voice_names:
        raise HTTPException(status_code=422, detail=f"No predefined voices for provider '{conn.provider}'")

    created = 0
    org_id = user.selected_organization_id
    for voice_name in voice_names:
        existing = await db_client.get_voice_by_provider_id(voice_name, org_id)
        if existing:
            continue
        await db_client.create_voice(
            user_id=user.id,
            organization_id=org_id,
            name=voice_name.capitalize(),
            provider=conn.provider,
            provider_voice_id=voice_name,
            is_public=True,
            status="ready",
            language="en",
            labels={"provider": conn.provider},
        )
        created += 1
        logger.info(f"Seeded voice '{voice_name}' ({conn.provider}) for org {org_id}")

    return {"created": created, "total": len(voice_names)}


@router.post("/connections/{connection_id}/sync-models")
async def sync_connection_models(connection_id: int, user=Depends(get_user)):
    """Re-fetch and reseed models for a connection from the live provider API (falls back to catalog)."""
    count = await db_client.reseed_models_for_connection(
        connection_id=connection_id,
        organization_id=user.selected_organization_id,
    )
    if count == 0:
        raise HTTPException(status_code=404, detail="Connection not found or no models available")
    return {"connection_id": connection_id, "model_count": count}


@router.put("/connections/{connection_id}", response_model=ProviderConnectionResponseSchema)
async def update_connection(
    connection_id: int,
    body: UpdateProviderConnectionSchema,
    user=Depends(get_user),
):
    """Update API key or config for an existing connection. Admin only."""
    conn = await db_client.update_connection(
        connection_id=connection_id,
        organization_id=user.selected_organization_id,
        api_key=body.api_key,
        extra_config=body.extra_config,
        display_name=body.display_name,
    )
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    return _conn_to_schema(conn)


@router.delete("/connections/{connection_id}")
async def delete_connection(connection_id: int, user=Depends(get_user)):
    """Soft-delete a provider connection and all its models. Admin only."""
    removed = await db_client.delete_connection(
        connection_id=connection_id,
        organization_id=user.selected_organization_id,
    )
    if not removed:
        raise HTTPException(status_code=404, detail="Connection not found")
    return {"success": True}


# ─── Admin + Client: Models ───────────────────────────────────────────────────

@router.get("/models", response_model=list[AvailableModelResponseSchema])
async def list_models(
    service_type: Optional[str] = None,
    user=Depends(get_user),
):
    """List all models. Superusers see all orgs; others see their own."""
    if user.is_superuser:
        conns = await db_client.list_all_connections_superuser()
        models = await db_client.list_all_available_models_superuser(service_type=service_type)
    else:
        conns = await db_client.list_connections(user.selected_organization_id)
        models = await db_client.list_available_models(
            user.selected_organization_id,
            service_type=service_type,
        )
    provider_by_conn = {c.id: c.provider for c in conns}
    return [_model_to_schema(m, provider_by_conn.get(m.connection_id, "")) for m in models]


@router.get("/client-models", response_model=list[AvailableModelResponseSchema])
async def list_client_models(
    service_type: Optional[str] = None,
    organization_id: Optional[int] = None,
    user=Depends(get_user),
):
    """List only models marked as client-available. Used by client model selector."""
    # Superusers can pass an explicit organization_id to view another org's models.
    if user.is_superuser and organization_id is not None:
        effective_org_id = organization_id
    else:
        effective_org_id = user.selected_organization_id

    conns = await db_client.list_connections(effective_org_id)
    provider_by_conn = {c.id: c.provider for c in conns}

    models = await db_client.list_available_models(
        effective_org_id,
        service_type=service_type,
        client_only=True,
    )
    return [_model_to_schema(m, provider_by_conn.get(m.connection_id, "")) for m in models]


@router.patch("/models/{model_pk}/availability", response_model=AvailableModelResponseSchema)
async def set_model_availability(
    model_pk: int,
    body: SetModelClientAvailableSchema,
    user=Depends(get_user),
):
    """Toggle whether a model is visible to clients. Admin only."""
    conns = await db_client.list_connections(user.selected_organization_id)
    provider_by_conn = {c.id: c.provider for c in conns}

    m = await db_client.set_model_client_available(
        model_id_pk=model_pk,
        organization_id=user.selected_organization_id,
        is_client_available=body.is_client_available,
    )
    if not m:
        raise HTTPException(status_code=404, detail="Model not found")
    return _model_to_schema(m, provider_by_conn.get(m.connection_id, ""))


@router.patch("/models/{model_pk}/default", response_model=AvailableModelResponseSchema)
async def set_model_default(
    model_pk: int,
    user=Depends(get_user),
):
    """Mark a model as the default for its service type (clears previous default). Admin only."""
    conns = await db_client.list_connections(user.selected_organization_id)
    provider_by_conn = {c.id: c.provider for c in conns}

    all_models = await db_client.list_available_models(user.selected_organization_id)
    target = next((x for x in all_models if x.id == model_pk), None)
    if not target:
        raise HTTPException(status_code=404, detail="Model not found")

    m = await db_client.set_model_default(
        model_id_pk=model_pk,
        organization_id=user.selected_organization_id,
        service_type=target.service_type,
    )
    return _model_to_schema(m, provider_by_conn.get(m.connection_id, ""))


@router.patch("/models/{model_pk}/display-name", response_model=AvailableModelResponseSchema)
async def set_model_display_name(
    model_pk: int,
    body: SetModelDisplayNameSchema,
    user=Depends(get_user),
):
    """Set a human-readable display name for a model. Pass null to clear."""
    conns = await db_client.list_connections(user.selected_organization_id)
    provider_by_conn = {c.id: c.provider for c in conns}

    m = await db_client.set_model_display_name(
        model_id_pk=model_pk,
        organization_id=user.selected_organization_id,
        display_name=body.display_name,
    )
    if not m:
        raise HTTPException(status_code=404, detail="Model not found")
    return _model_to_schema(m, provider_by_conn.get(m.connection_id, ""))


@router.patch("/models/{model_pk}/our-price", response_model=AvailableModelResponseSchema)
async def set_model_our_price(
    model_pk: int,
    body: SetModelOurPriceSchema,
    user=Depends(get_user),
):
    """Set the 'our price per minute' for a model. Admin only."""
    conns = await db_client.list_connections(user.selected_organization_id)
    provider_by_conn = {c.id: c.provider for c in conns}

    m = await db_client.set_model_our_price(
        model_id_pk=model_pk,
        organization_id=user.selected_organization_id,
        our_price_per_min_usd=body.our_price_per_min_usd,
    )
    if not m:
        raise HTTPException(status_code=404, detail="Model not found")
    return _model_to_schema(m, provider_by_conn.get(m.connection_id, ""))


# ─── Pricing ──────────────────────────────────────────────────────────────────

@router.post("/pricing/refresh")
async def refresh_pricing(user=Depends(get_user)):
    """Populate cost_per_min_usd and native_cost_display for all models from the pricing catalog."""
    models = await db_client.list_available_models(user.selected_organization_id)
    upserted = 0
    for m in models:
        pricing = _get_pricing(m.model_id)
        if pricing is None:
            continue
        cost_per_min, native_display = pricing
        await db_client.set_model_cost(
            model_id_pk=m.id,
            organization_id=user.selected_organization_id,
            cost_per_min_usd=cost_per_min,
            native_cost_display=native_display,
        )
        upserted += 1
    logger.info(f"Pricing refresh: populated {upserted}/{len(models)} models for org {user.selected_organization_id}")
    return {"upserted": upserted, "total": len(models)}


@router.post("/pricing/apply-margin")
async def apply_margin(body: ApplyMarginSchema, user=Depends(get_user)):
    """Calculate our_price_per_min = cost_per_min * (1 + margin/100) for all priced models."""
    models = await db_client.list_available_models(user.selected_organization_id)
    if body.connection_id is not None:
        models = [m for m in models if m.connection_id == body.connection_id]

    multiplier = 1.0 + (body.margin_percent / 100.0)
    updated = 0
    for m in models:
        if m.cost_per_min_usd is None:
            continue
        our_price = round(m.cost_per_min_usd * multiplier, 6)
        await db_client.set_model_our_price(
            model_id_pk=m.id,
            organization_id=user.selected_organization_id,
            our_price_per_min_usd=our_price,
        )
        updated += 1
    return {"updated": updated}
