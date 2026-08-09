"""
Pricing accuracy regressions, all found 2026-08-09 against live cost data.

Each test here corresponds to a way the cost of a real call was wrong, not to a
hypothetical. The theme is the same throughout: a missing or assumed rate
produced a confident number rather than a visible gap.
"""

from decimal import Decimal

from api.services.configuration.registry import ServiceProviders
from api.services.pricing.cost_calculator import cost_calculator
from api.services.pricing.llm import LLM_PRICING
from api.services.pricing.models import TokenPricingModel


class TestRealtimeIsPricedAsAudio:
    """Every token through a realtime model is audio, and audio costs ~25x text."""

    def test_realtime_uses_audio_rates_not_text_rates(self):
        model = LLM_PRICING[ServiceProviders.OPENAI]["gpt-4o-realtime-preview"]
        # $32/1M audio in, not the $5/1M text rate it used to carry.
        assert model.prompt_token_price == Decimal("32.00") / 1000000
        assert model.completion_token_price == Decimal("64.00") / 1000000

    def test_mini_realtime_too(self):
        model = LLM_PRICING[ServiceProviders.OPENAI]["gpt-4o-mini-realtime-preview"]
        assert model.prompt_token_price == Decimal("32.00") / 1000000

    def test_a_voice_minute_is_not_priced_as_text(self):
        """The understatement this closes, in money."""
        audio = LLM_PRICING[ServiceProviders.OPENAI]["gpt-4o-realtime-preview"]
        text = LLM_PRICING[ServiceProviders.OPENAI]["gpt-4o"]
        usage = {"prompt_tokens": 100_000, "completion_tokens": 20_000}
        # Audio pricing must be materially dearer; if these ever converge,
        # someone has pasted a text rate into a realtime entry again.
        assert audio.calculate_cost(usage) > text.calculate_cost(usage) * 4


class TestAnthropicCacheSemantics:
    """Claude reports cache reads OUTSIDE prompt_tokens, and publishes the rate."""

    def test_cached_tokens_are_charged_not_discounted(self):
        model = LLM_PRICING[ServiceProviders.ANTHROPIC]["claude-sonnet-5"]
        # 1M cached reads, no fresh input at all.
        cost = model.calculate_cost(
            {"prompt_tokens": 0, "completion_tokens": 0, "cache_read_input_tokens": 1_000_000}
        )
        # $0.20 / MTok — a real charge. The old model subtracted a "saving"
        # here and returned 0, i.e. a cache-heavy call appeared free.
        assert cost == Decimal("0.20")

    def test_cache_reads_do_not_drive_cost_negative(self):
        model = LLM_PRICING[ServiceProviders.ANTHROPIC]["claude-opus-5"]
        cost = model.calculate_cost(
            {"prompt_tokens": 10, "completion_tokens": 0, "cache_read_input_tokens": 5_000_000}
        )
        assert cost > 0

    def test_all_anthropic_entries_declare_the_semantics(self):
        for name, model in LLM_PRICING[ServiceProviders.ANTHROPIC].items():
            assert model.cached_tokens_included_in_prompt is False, name
            assert model.cached_prompt_token_price is not None, name


class TestOpenAICacheSemantics:
    """OpenAI includes cached tokens inside prompt_tokens, at 10% on GPT-5."""

    def test_cached_tokens_are_removed_from_the_full_price_bucket(self):
        model = LLM_PRICING[ServiceProviders.OPENAI]["gpt-5"]
        cost = model.calculate_cost(
            {
                "prompt_tokens": 1_000_000,
                "completion_tokens": 0,
                "cache_read_input_tokens": 1_000_000,
            }
        )
        # All input was cached: 1M x $0.125/1M, not 1M x $1.25/1M, and not
        # $1.25 minus a 50% "discount" ($0.625) as the old default gave.
        assert cost == Decimal("0.125")


class TestUnpricedModelsAreReported:
    """A model with no rate costs zero — that must never be silent again."""

    def test_unknown_model_is_named_in_the_breakdown(self):
        result = cost_calculator.calculate_total_cost(
            {"llm": {"someproc|||a-model-that-does-not-exist": {
                "prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}}}
        )
        assert result["total"] == 0
        assert "unpriced" in result, "a zero with no explanation is the bug"
        assert any("a-model-that-does-not-exist" in e for e in result["unpriced"])

    def test_a_priced_model_reports_no_gap(self):
        result = cost_calculator.calculate_total_cost(
            {"llm": {"someproc|||gpt-4o-mini": {
                "prompt_tokens": 1_000, "completion_tokens": 1_000}}}
        )
        assert result["total"] > 0
        assert "unpriced" not in result


class TestProvidersThatUsedToCostNothing:
    """Anthropic and Google had no entries at all, so priced at exactly zero."""

    def test_anthropic_is_priced(self):
        assert LLM_PRICING.get(ServiceProviders.ANTHROPIC)

    def test_google_is_priced(self):
        assert LLM_PRICING.get(ServiceProviders.GOOGLE)

    def test_a_claude_call_costs_something(self):
        model = LLM_PRICING[ServiceProviders.ANTHROPIC]["claude-sonnet-5"]
        assert model.calculate_cost(
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
        ) == Decimal("12.00")  # $2 in + $10 out


class TestAudioSplitWhenReported:
    """If a provider ever does report an audio split, price it at the audio rate."""

    def test_audio_tokens_billed_separately_and_not_twice(self):
        model = TokenPricingModel(
            prompt_token_price=Decimal("0.30") / 1000000,
            completion_token_price=Decimal("2.50") / 1000000,
            audio_prompt_token_price=Decimal("1.00") / 1000000,
        )
        cost = model.calculate_cost(
            {"prompt_tokens": 1_000_000, "audio_prompt_tokens": 400_000, "completion_tokens": 0}
        )
        # 600k text @ $0.30/1M + 400k audio @ $1.00/1M
        assert cost == Decimal("0.18") + Decimal("0.40")

    def test_absent_audio_counts_change_nothing(self):
        model = TokenPricingModel(
            prompt_token_price=Decimal("0.30") / 1000000,
            completion_token_price=Decimal("2.50") / 1000000,
            audio_prompt_token_price=Decimal("1.00") / 1000000,
        )
        # No split reported: must not invent one or drop to the audio rate.
        assert model.calculate_cost(
            {"prompt_tokens": 1_000_000, "completion_tokens": 0}
        ) == Decimal("0.30")
