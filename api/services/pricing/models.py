"""
Base pricing models for different service types.
"""

from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional


class CostType(Enum):
    LLM_TOKENS = "llm_tokens"
    TTS_CHARACTERS = "tts_characters"
    STT_SECONDS = "stt_seconds"


class PricingModel:
    """Base class for pricing models"""

    def calculate_cost(self, usage: Any) -> Decimal:
        """Calculate cost based on usage"""
        raise NotImplementedError


class TokenPricingModel(PricingModel):
    """
    Pricing model for token-based services (LLM).

    CACHED TOKENS — two things providers disagree about
    ---------------------------------------------------
    This used to model a cache read as a *discount subtracted from* the prompt
    cost, at a flat 50%. Both halves of that were wrong for current models:

    1. THE RATE. 50% was the GPT-4o-era figure. A cache read is now 10% of the
       input price on Claude and on GPT-5 — a 90% discount, not 50%. Guessing a
       multiplier when providers publish an exact cached price is a choice to be
       approximately wrong, so `cached_prompt_token_price` takes the published
       figure directly and the multiplier is only a fallback.

    2. WHETHER THEY ARE ALREADY COUNTED. Subtracting a discount is only correct
       if the cached tokens are also inside `prompt_tokens`. OpenAI includes
       them; **Anthropic reports them separately**. So on Claude the old code
       deducted a saving for tokens it had never charged for, pushing the cost
       below the truth (and, with enough cache hits, to the max(...,0) floor).
       `cached_tokens_included_in_prompt` makes that provider difference
       explicit instead of assumed.

    AUDIO TOKENS
    ------------
    Audio bills far above text — $32/1M vs $1.25/1M on OpenAI realtime, $1.00
    vs $0.30 on Gemini Flash. The pipeline's usage metrics carry no audio/text
    split (`LLMTokenUsage` has only prompt/completion/cache counts), so audio
    cannot be separated from a mixed response.

    It does not need to be for the case that matters: on a realtime model every
    token IS audio, so such a model is registered with audio rates as its
    prompt/completion price. `audio_prompt_token_price` exists for the other
    case — a provider that does report an audio split — and is used only when
    the usage dict actually carries those counts, so it can never silently
    double-charge a model that doesn't.
    """

    def __init__(
        self,
        prompt_token_price: Decimal,
        completion_token_price: Decimal,
        cache_read_discount: Decimal = Decimal("0.5"),
        cache_creation_multiplier: Decimal = Decimal("1.25"),
        *,
        cached_prompt_token_price: Optional[Decimal] = None,
        cached_tokens_included_in_prompt: bool = True,
        audio_prompt_token_price: Optional[Decimal] = None,
        audio_completion_token_price: Optional[Decimal] = None,
    ):
        self.prompt_token_price = prompt_token_price
        self.completion_token_price = completion_token_price
        self.cache_read_discount = cache_read_discount
        self.cache_creation_multiplier = cache_creation_multiplier
        self.cached_prompt_token_price = cached_prompt_token_price
        self.cached_tokens_included_in_prompt = cached_tokens_included_in_prompt
        self.audio_prompt_token_price = audio_prompt_token_price
        self.audio_completion_token_price = audio_completion_token_price

    def calculate_cost(self, usage: Dict[str, int]) -> Decimal:
        """Calculate cost for LLM token usage"""
        prompt_tokens = usage.get("prompt_tokens", 0) or 0
        completion_tokens = usage.get("completion_tokens", 0) or 0
        cache_read_tokens = usage.get("cache_read_input_tokens") or 0
        cache_creation_tokens = usage.get("cache_creation_input_tokens") or 0

        # Only used when the provider actually reports a split. Absent keys mean
        # "not reported", never "zero audio" — a realtime model reports no split
        # and is priced through prompt/completion at its audio rate instead.
        audio_prompt_tokens = (
            usage.get("audio_prompt_tokens")
            or usage.get("input_audio_tokens")
            or 0
        )
        audio_completion_tokens = (
            usage.get("audio_completion_tokens")
            or usage.get("output_audio_tokens")
            or 0
        )

        billable_prompt = prompt_tokens
        billable_completion = completion_tokens
        audio_cost = Decimal("0")

        if audio_prompt_tokens and self.audio_prompt_token_price is not None:
            audio_cost += Decimal(audio_prompt_tokens) * self.audio_prompt_token_price
            # Providers that report a split count it inside the total, so the
            # audio share is removed from the text bucket rather than billed twice.
            billable_prompt = max(prompt_tokens - audio_prompt_tokens, 0)

        if audio_completion_tokens and self.audio_completion_token_price is not None:
            audio_cost += (
                Decimal(audio_completion_tokens) * self.audio_completion_token_price
            )
            billable_completion = max(completion_tokens - audio_completion_tokens, 0)

        cache_read_cost = Decimal("0")

        if self.cached_prompt_token_price is not None:
            # Published cached rate: charge cached tokens at it directly, and
            # take them out of the full-price bucket if they were counted there.
            cache_read_cost = (
                Decimal(cache_read_tokens) * self.cached_prompt_token_price
            )
            if self.cached_tokens_included_in_prompt:
                billable_prompt = max(billable_prompt - cache_read_tokens, 0)
        elif self.cached_tokens_included_in_prompt:
            # Fallback for models with no published cached rate: the legacy
            # discount, kept only so unmigrated entries behave as before.
            cache_read_cost = -(
                Decimal(cache_read_tokens)
                * self.prompt_token_price
                * self.cache_read_discount
            )
        else:
            # Reported separately with no cached rate — charge at full input
            # price. Overstates slightly, which is the safe direction: an
            # inflated cost is visible and arguable, a silently understated one
            # is not.
            cache_read_cost = Decimal(cache_read_tokens) * self.prompt_token_price

        prompt_cost = Decimal(billable_prompt) * self.prompt_token_price
        completion_cost = Decimal(billable_completion) * self.completion_token_price

        cache_creation_cost = (
            Decimal(cache_creation_tokens)
            * self.prompt_token_price
            * self.cache_creation_multiplier
            if not self.cached_tokens_included_in_prompt
            # When cache-creation tokens are already inside prompt_tokens, only
            # the premium above the base rate is still owed.
            else Decimal(cache_creation_tokens)
            * self.prompt_token_price
            * (self.cache_creation_multiplier - 1)
        )

        total_cost = (
            prompt_cost
            + completion_cost
            + audio_cost
            + cache_read_cost
            + cache_creation_cost
        )
        return max(total_cost, Decimal("0"))  # Ensure non-negative


class CharacterPricingModel(PricingModel):
    """Pricing model for character-based services (TTS)"""

    def __init__(self, character_price: Decimal):
        self.character_price = character_price

    def calculate_cost(self, character_count: int) -> Decimal:
        """Calculate cost for TTS character usage"""
        return Decimal(character_count) * self.character_price


class TimePricingModel(PricingModel):
    """Pricing model for time-based services (STT)"""

    def __init__(self, second_price: Decimal):
        self.second_price = second_price

    def calculate_cost(self, seconds: float) -> Decimal:
        """Calculate cost for STT time usage"""
        return Decimal(str(seconds)) * self.second_price
