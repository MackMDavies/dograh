"""
LLM pricing models for different providers.

Prices are per 1000 tokens for most models, with some newer models priced per million tokens.

RATES_ASOF — read 2026-08-09 from each provider's own pricing page:
  OpenAI     https://developers.openai.com/api/docs/pricing
  Anthropic  https://platform.claude.com/docs/en/about-claude/pricing
  Google     https://ai.google.dev/gemini-api/docs/pricing

Anything without a rate here costs ZERO in cost_calculator, silently, and that
zero reaches the customer-facing run total. Until this date the table had no
Anthropic rates at all and nothing newer than GPT-4o, so every Claude call and
every current-generation model was recorded as free. Adding a model is not
optional tidiness — it is the difference between a cost and a fiction.

When a provider reprices, update the figure AND this date. A rate nobody can
date is a rate nobody can check.
"""

from decimal import Decimal
from typing import Dict

from api.services.configuration.registry import ServiceProviders

from .models import TokenPricingModel

# LLM pricing registry
LLM_PRICING: Dict[str, Dict[str, TokenPricingModel]] = {
    ServiceProviders.OPENAI: {
        "gpt-3.5-turbo": TokenPricingModel(
            prompt_token_price=Decimal("0.0015") / 1000,  # $0.0015 per 1K tokens
            completion_token_price=Decimal("0.002") / 1000,  # $0.002 per 1K tokens
        ),
        "gpt-4": TokenPricingModel(
            prompt_token_price=Decimal("0.03") / 1000,  # $0.03 per 1K tokens
            completion_token_price=Decimal("0.06") / 1000,  # $0.06 per 1K tokens
        ),
        # Current generation, added 2026-08-09. Absent before that, so any run
        # on a GPT-5 model recorded a cost of exactly zero.
        "gpt-5": TokenPricingModel(
            prompt_token_price=Decimal("1.25") / 1000000,  # $1.25 per 1M tokens
            completion_token_price=Decimal("10.00") / 1000000,  # $10.00 per 1M tokens
        ),
        "gpt-5-mini": TokenPricingModel(
            prompt_token_price=Decimal("0.25") / 1000000,  # $0.25 per 1M tokens
            completion_token_price=Decimal("2.00") / 1000000,  # $2.00 per 1M tokens
        ),
        "gpt-5.4": TokenPricingModel(
            prompt_token_price=Decimal("2.50") / 1000000,  # $2.50 per 1M tokens
            completion_token_price=Decimal("15.00") / 1000000,  # $15.00 per 1M tokens
        ),
        "gpt-5.4-mini": TokenPricingModel(
            prompt_token_price=Decimal("0.75") / 1000000,  # $0.75 per 1M tokens
            completion_token_price=Decimal("4.50") / 1000000,  # $4.50 per 1M tokens
        ),
        "gpt-5.5": TokenPricingModel(
            prompt_token_price=Decimal("5.00") / 1000000,  # $5.00 per 1M tokens
            completion_token_price=Decimal("30.00") / 1000000,  # $30.00 per 1M tokens
        ),
        "gpt-4.1": TokenPricingModel(
            prompt_token_price=Decimal("2.00") / 1000000,  # $2.00 per 1M tokens
            completion_token_price=Decimal("8.00") / 1000000,  # $8.00 per 1M tokens
        ),
        "gpt-4.1-mini": TokenPricingModel(
            prompt_token_price=Decimal("0.40") / 1000000,  # $0.40 per 1M tokens
            completion_token_price=Decimal("1.60") / 1000000,  # $1.60 per 1M tokens
        ),
        "gpt-4.1-nano": TokenPricingModel(
            prompt_token_price=Decimal("0.10") / 1000000,  # $0.10 per 1M tokens
            completion_token_price=Decimal("0.40") / 1000000,  # $0.40 per 1M tokens
        ),
        "gpt-4.5-preview": TokenPricingModel(
            prompt_token_price=Decimal("75.00") / 1000000,  # $75.00 per 1M tokens
            completion_token_price=Decimal("150.00") / 1000000,  # $150.00 per 1M tokens
        ),
        "gpt-4o": TokenPricingModel(
            prompt_token_price=Decimal("2.50") / 1000000,  # $2.50 per 1M tokens - FIXED
            completion_token_price=Decimal("10.00")
            / 1000000,  # $10.00 per 1M tokens - FIXED
        ),
        "gpt-4o-audio-preview": TokenPricingModel(
            prompt_token_price=Decimal("2.50") / 1000000,  # $2.50 per 1M tokens
            completion_token_price=Decimal("10.00") / 1000000,  # $10.00 per 1M tokens
        ),
        "gpt-4o-realtime-preview": TokenPricingModel(
            prompt_token_price=Decimal("5.00") / 1000000,  # $5.00 per 1M tokens
            completion_token_price=Decimal("20.00") / 1000000,  # $20.00 per 1M tokens
        ),
        "gpt-4o-mini": TokenPricingModel(
            prompt_token_price=Decimal("0.15") / 1000000,  # $0.15 per 1M tokens
            completion_token_price=Decimal("0.60") / 1000000,  # $0.60 per 1M tokens
        ),
        "gpt-4o-mini-audio-preview": TokenPricingModel(
            prompt_token_price=Decimal("0.15") / 1000000,  # $0.15 per 1M tokens
            completion_token_price=Decimal("0.60") / 1000000,  # $0.60 per 1M tokens
        ),
        "gpt-4o-mini-realtime-preview": TokenPricingModel(
            prompt_token_price=Decimal("0.60") / 1000000,  # $0.60 per 1M tokens
            completion_token_price=Decimal("2.40") / 1000000,  # $2.40 per 1M tokens
        ),
        "gpt-4o-search-preview": TokenPricingModel(
            prompt_token_price=Decimal("2.50") / 1000000,  # $2.50 per 1M tokens
            completion_token_price=Decimal("10.00") / 1000000,  # $10.00 per 1M tokens
        ),
        "gpt-4o-mini-search-preview": TokenPricingModel(
            prompt_token_price=Decimal("0.15") / 1000000,  # $0.15 per 1M tokens
            completion_token_price=Decimal("0.60") / 1000000,  # $0.60 per 1M tokens
        ),
        "o1": TokenPricingModel(
            prompt_token_price=Decimal("15.00") / 1000000,  # $15.00 per 1M tokens
            completion_token_price=Decimal("60.00") / 1000000,  # $60.00 per 1M tokens
        ),
        "o1-pro": TokenPricingModel(
            prompt_token_price=Decimal("150.00") / 1000000,  # $150.00 per 1M tokens
            completion_token_price=Decimal("600.00") / 1000000,  # $600.00 per 1M tokens
        ),
        "o1-mini": TokenPricingModel(
            prompt_token_price=Decimal("1.10") / 1000000,  # $1.10 per 1M tokens
            completion_token_price=Decimal("4.40") / 1000000,  # $4.40 per 1M tokens
        ),
        "o3": TokenPricingModel(
            prompt_token_price=Decimal("10.00") / 1000000,  # $10.00 per 1M tokens
            completion_token_price=Decimal("40.00") / 1000000,  # $40.00 per 1M tokens
        ),
        "o3-mini": TokenPricingModel(
            prompt_token_price=Decimal("1.10") / 1000000,  # $1.10 per 1M tokens
            completion_token_price=Decimal("4.40") / 1000000,  # $4.40 per 1M tokens
        ),
        "o4-mini": TokenPricingModel(
            prompt_token_price=Decimal("1.10") / 1000000,  # $1.10 per 1M tokens
            completion_token_price=Decimal("4.40") / 1000000,  # $4.40 per 1M tokens
        ),
        "computer-use-preview": TokenPricingModel(
            prompt_token_price=Decimal("3.00") / 1000000,  # $3.00 per 1M tokens
            completion_token_price=Decimal("12.00") / 1000000,  # $12.00 per 1M tokens
        ),
        "gpt-image-1": TokenPricingModel(
            prompt_token_price=Decimal("5.00") / 1000000,  # $5.00 per 1M tokens
            completion_token_price=Decimal("0") / 1000000,  # No output pricing shown
        ),
        "codex-mini-latest": TokenPricingModel(
            prompt_token_price=Decimal("1.50") / 1000000,  # $1.50 per 1M tokens
            completion_token_price=Decimal("6.00") / 1000000,  # $6.00 per 1M tokens
        ),
        # Transcription models
        "gpt-4o-transcribe": TokenPricingModel(
            prompt_token_price=Decimal("2.50") / 1000000,  # $2.50 per 1M tokens
            completion_token_price=Decimal("10.00") / 1000000,  # $10.00 per 1M tokens
        ),
        "gpt-4o-mini-transcribe": TokenPricingModel(
            prompt_token_price=Decimal("1.25") / 1000000,  # $1.25 per 1M tokens
            completion_token_price=Decimal("5.00") / 1000000,  # $5.00 per 1M tokens
        ),
        # TTS models with token-based pricing
        "gpt-4o-mini-tts": TokenPricingModel(
            prompt_token_price=Decimal("0.60") / 1000000,  # $0.60 per 1M tokens
            completion_token_price=Decimal("0")
            / 1000000,  # No completion tokens for TTS
        ),
    },
    # ── Anthropic ──────────────────────────────────────────────────────────
    # Absent entirely until 2026-08-09, so every Claude call cost zero.
    # platform.claude.com/docs/en/about-claude/pricing
    ServiceProviders.ANTHROPIC: {
        "claude-opus-5": TokenPricingModel(
            prompt_token_price=Decimal("5.00") / 1000000,  # $5 / MTok
            completion_token_price=Decimal("25.00") / 1000000,  # $25 / MTok
        ),
        "claude-opus-4-5": TokenPricingModel(
            prompt_token_price=Decimal("5.00") / 1000000,
            completion_token_price=Decimal("25.00") / 1000000,
        ),
        # Introductory pricing, $2/$10, ENDS 2026-08-31 — reverts to $3/$15.
        # Left at the introductory rate deliberately: it is correct today, and a
        # future rate applied early would overstate every call until September.
        "claude-sonnet-5": TokenPricingModel(
            prompt_token_price=Decimal("2.00") / 1000000,  # $2 / MTok (intro)
            completion_token_price=Decimal("10.00") / 1000000,  # $10 / MTok (intro)
        ),
        "claude-sonnet-4-5": TokenPricingModel(
            prompt_token_price=Decimal("3.00") / 1000000,  # $3 / MTok
            completion_token_price=Decimal("15.00") / 1000000,  # $15 / MTok
        ),
        "claude-haiku-4-5": TokenPricingModel(
            prompt_token_price=Decimal("1.00") / 1000000,  # $1 / MTok
            completion_token_price=Decimal("5.00") / 1000000,  # $5 / MTok
        ),
    },
    # ── Google Gemini ──────────────────────────────────────────────────────
    # Also absent until 2026-08-09. ai.google.dev/gemini-api/docs/pricing
    #
    # NOTE: Gemini prices AUDIO input separately and higher than text —
    # $1.00/1M vs $0.30/1M on 2.5 Flash. TokenPricingModel has one input rate,
    # so a voice pipeline sending audio tokens is understated ~3x here. Fixing
    # that needs a model change, not a rate change; recorded so the limitation
    # is known rather than discovered.
    ServiceProviders.GOOGLE: {
        "gemini-2.5-flash": TokenPricingModel(
            prompt_token_price=Decimal("0.30") / 1000000,  # $0.30 / 1M text in
            completion_token_price=Decimal("2.50") / 1000000,  # $2.50 / 1M out
        ),
        "gemini-2.5-flash-lite": TokenPricingModel(
            prompt_token_price=Decimal("0.10") / 1000000,
            completion_token_price=Decimal("0.40") / 1000000,
        ),
        "gemini-2.5-pro": TokenPricingModel(
            prompt_token_price=Decimal("1.25") / 1000000,  # <=200k context
            completion_token_price=Decimal("10.00") / 1000000,
        ),
        "gemini-3.5-flash": TokenPricingModel(
            prompt_token_price=Decimal("1.50") / 1000000,
            completion_token_price=Decimal("9.00") / 1000000,
        ),
        "gemini-3.5-flash-lite": TokenPricingModel(
            prompt_token_price=Decimal("0.30") / 1000000,
            completion_token_price=Decimal("2.50") / 1000000,
        ),
    },
    ServiceProviders.GROQ: {
        "llama-3.3-70b-versatile": TokenPricingModel(
            prompt_token_price=Decimal("0.00059") / 1000,  # $0.00059 per 1K tokens
            completion_token_price=Decimal("0.00079") / 1000,  # $0.00079 per 1K tokens
        ),
        "deepseek-r1-distill-llama-70b": TokenPricingModel(
            prompt_token_price=Decimal("0.00059") / 1000,  # Assuming similar pricing
            completion_token_price=Decimal("0.00079") / 1000,
        ),
    },
    ServiceProviders.AZURE: {
        # The figures here disagreed with their own comments — 0.44 beside
        # "$0.40", and 8.80 beside "$1.60" — so one of the two was wrong and
        # nothing recorded which. The output rate is the alarming one: 8.80 is
        # 5.5x the commented 1.60, and it has been multiplying every Azure
        # completion token.
        #
        # Aligned to the commented intent, which matches OpenAI's own
        # gpt-4.1-mini list price ($0.40 / $1.60 per 1M) — Azure resells the
        # same model. Flagged rather than silently kept: if this account is on
        # a data-zone deployment the real rates are higher, and they should be
        # taken from the Azure price list rather than inferred here.
        "gpt-4.1-mini": TokenPricingModel(
            prompt_token_price=Decimal("0.40") / 1000000,  # $0.40 per 1M tokens
            completion_token_price=Decimal("1.60") / 1000000,  # $1.60 per 1M tokens
        )
    },
}
