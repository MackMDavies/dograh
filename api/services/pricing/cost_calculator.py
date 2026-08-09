"""
Cost Calculator for Workflow Runs

This module provides a comprehensive cost calculation system for workflow runs based on usage metrics
from different AI service providers (OpenAI, Groq, Deepgram, etc.).

Features:
- Token-based pricing for LLM services with cache optimization support
- Character-based pricing for TTS services
- Time-based pricing for STT services
- Configurable pricing models that can be updated
- Support for multiple providers and models
- Automatic provider inference from model names
- JSON serialization support for database storage

Usage:
    from api.tasks.cost_calculator import cost_calculator

    usage_info = {
        "llm": {
            "processor_name|||gpt-4o": {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "total_tokens": 1500,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0
            }
        },
        "tts": {
            "processor_name|||aura-2-helena-en": 2000  # character count
        }
    }

    cost_breakdown = cost_calculator.calculate_total_cost(usage_info)
    print(f"Total cost: ${cost_breakdown['total']:.6f}")
"""

from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from loguru import logger

from api.services.configuration.registry import ServiceProviders
from api.services.pricing import PRICING_REGISTRY
from api.services.pricing.models import (
    PricingModel,
)


class CostCalculator:
    """Main cost calculator class"""

    def __init__(self, pricing_registry: Dict = None):
        self.pricing_registry = pricing_registry or PRICING_REGISTRY

    def get_pricing_model(
        self, service_type: str, provider: str, model: str
    ) -> Optional[PricingModel]:
        """Get pricing model for a specific service, provider, and model"""
        try:
            service_pricing = self.pricing_registry.get(service_type, {})

            # Try to get pricing for the specific provider
            provider_pricing = service_pricing.get(provider, {})
            pricing_model = provider_pricing.get(model) or provider_pricing.get(
                "default"
            )

            if pricing_model:
                return pricing_model

            # If not found, try the "default" provider for this service type
            default_provider_pricing = service_pricing.get("default", {})
            return default_provider_pricing.get(model) or default_provider_pricing.get(
                "default"
            )

        except (KeyError, AttributeError):
            return None

    def calculate_llm_cost(
        self, provider: str, model: str, usage: Dict[str, int]
    ) -> Decimal:
        """Calculate cost for LLM usage"""
        pricing_model = self.get_pricing_model("llm", provider, model)
        if not pricing_model:
            return Decimal("0")
        return pricing_model.calculate_cost(usage)

    def calculate_tts_cost(
        self, provider: str, model: str, character_count: int
    ) -> Decimal:
        """Calculate cost for TTS usage"""
        pricing_model = self.get_pricing_model("tts", provider, model)
        if not pricing_model:
            return Decimal("0")
        return pricing_model.calculate_cost(character_count)

    def calculate_stt_cost(self, provider: str, model: str, seconds: float) -> Decimal:
        """Calculate cost for STT usage"""
        pricing_model = self.get_pricing_model("stt", provider, model)
        if not pricing_model:
            return Decimal("0")
        return pricing_model.calculate_cost(seconds)

    def calculate_total_cost(self, usage_info: Dict) -> Dict[str, Any]:
        llm_cost_total = Decimal("0")
        tts_cost_total = Decimal("0")
        stt_cost_total = Decimal("0")

        # Models we had no rate for. Their usage is REAL and its cost is UNKNOWN,
        # not zero — see the note on `unpriced` in the returned dict.
        unpriced: list[str] = []

        def _note_if_unpriced(service: str, provider: str, model: str) -> None:
            if not self.get_pricing_model(service, provider, model):
                entry = f"{service}:{provider}/{model}"
                if entry not in unpriced:
                    unpriced.append(entry)

        # Calculate LLM costs
        llm_usage = usage_info.get("llm", {})
        for key, usage in llm_usage.items():
            processor, model = self._parse_key(key)
            # Processor first, model second. Azure resells OpenAI models under
            # identical names, so `gpt-4.1-mini` is undecidable from the model
            # alone and would always be priced as OpenAI. The processor is the
            # only signal that separates them; it is used only when it actually
            # identifies a provider, so a generic processor still falls through
            # to the model name.
            provider = self._infer_provider_from_processor(processor, "llm")
            if provider == "unknown":
                provider = self._infer_provider_from_model(model, "llm")
            _note_if_unpriced("llm", provider, model)
            cost = self.calculate_llm_cost(provider, model, usage)
            llm_cost_total += cost

        # Calculate TTS costs
        tts_usage = usage_info.get("tts", {})
        for key, character_count in tts_usage.items():
            processor, model = self._parse_key(key)
            # Handle the case where model is "None" - infer from processor
            if model.lower() in ["none", "null", ""]:
                provider = self._infer_provider_from_processor(processor, "tts")
                model = "default"  # Use default model for the provider
            else:
                provider = self._infer_provider_from_model(model, "tts")
            _note_if_unpriced("tts", provider, model)
            cost = self.calculate_tts_cost(provider, model, character_count)
            tts_cost_total += cost

        # Calculate STT costs from explicit stt usage
        stt_usage = usage_info.get("stt", {})
        for key, seconds in stt_usage.items():
            processor, model = self._parse_key(key)
            provider = self._infer_provider_from_model(model, "stt")
            _note_if_unpriced("stt", provider, model)
            cost = self.calculate_stt_cost(provider, model, seconds)
            stt_cost_total += cost

        if unpriced:
            logger.warning(
                f"[pricing] no rate for {', '.join(unpriced)} — their usage is "
                f"costed at 0 and the run total is understated"
            )

        total_cost = llm_cost_total + tts_cost_total + stt_cost_total

        result = {
            "llm_cost": float(llm_cost_total),
            "tts_cost": float(tts_cost_total),
            "stt_cost": float(stt_cost_total),
            "total": float(total_cost),
        }

        # A model with no rate contributes 0 to the totals above, and that zero
        # is indistinguishable from "this call genuinely cost nothing". It is
        # not the same claim at all, and the difference has been expensive:
        # LLM_PRICING carried no Anthropic rates whatsoever and nothing newer
        # than GPT-4o, so every Claude call and every current-generation model
        # priced at exactly nothing and the run total said so with a straight
        # face. Recording the gap here carries it into cost_info, so a total
        # that is missing components can say which ones rather than simply
        # reading low.
        if unpriced:
            result["unpriced"] = unpriced

        return result

    def _parse_key(self, key) -> Tuple[str, str]:
        """Parse key which is in format 'processor|||model'"""
        if isinstance(key, str) and "|||" in key:
            parts = key.split("|||", 1)
            return parts[0], parts[1]
        else:
            # Fallback for backwards compatibility or malformed keys
            return str(key), "unknown"

    def _infer_provider_from_model(self, model: str, service_type: str) -> str:
        """Infer provider from model name"""
        if not model:
            return "unknown"

        model_lower = model.lower()

        # Anthropic models. Absent until 2026-08-09, which made the Claude rates
        # added that day UNREACHABLE: `claude-opus-5` fell through to the
        # first-provider default below, was looked up under OPENAI, found
        # nothing, and cost zero. The rates existed; the lookup never saw them.
        if any(keyword in model_lower for keyword in ["claude", "anthropic"]):
            return ServiceProviders.ANTHROPIC

        # Google models — same story as Anthropic.
        if any(keyword in model_lower for keyword in ["gemini", "google", "palm"]):
            return ServiceProviders.GOOGLE

        # OpenAI models. The o-series carries no "gpt" in its name, so it must be
        # matched explicitly — it used to reach OpenAI only by accident of the
        # first-provider default, which is now removed.
        if any(
            keyword in model_lower
            for keyword in [
                "gpt", "whisper", "openai", "codex", "davinci", "tts-1",
                "computer-use",
            ]
        ):
            return ServiceProviders.OPENAI
        if model_lower.startswith(("o1", "o3", "o4")):
            return ServiceProviders.OPENAI

        # Groq models
        if any(
            keyword in model_lower
            for keyword in ["groq", "llama", "deepseek", "mixtral"]
        ):
            return ServiceProviders.GROQ

        # Elevenlabs models
        if any(keyword in model_lower for keyword in ["eleven"]):
            return ServiceProviders.ELEVENLABS

        # Cartesia models
        if any(keyword in model_lower for keyword in ["cartesia", "sonic"]):
            return ServiceProviders.CARTESIA

        # Deepgram models. "aura" is their TTS voice family.
        if any(
            keyword in model_lower
            for keyword in ["deepgram", "nova", "phonecall", "general", "aura", "flux"]
        ):
            return ServiceProviders.DEEPGRAM

        # NO DEFAULT PROVIDER.
        #
        # This used to return the first provider registered for the service type
        # — in practice always OPENAI — which meant an unrecognised model was
        # silently attributed to the wrong provider, missed, and priced at zero.
        # That is how Anthropic and Google rates could be added, verified present
        # by grep, and still never used.
        #
        # Returning "unknown" makes the miss explicit: the lookup fails, the
        # model is reported through cost_info.unpriced, and someone can add the
        # keyword. A wrong provider is worse than no provider, because a wrong
        # provider can silently find a same-named model and price against it.
        return "unknown"

    def _infer_provider_from_processor(self, processor: str, service_type: str) -> str:
        """Infer provider from processor name"""
        if not processor:
            return "unknown"

        processor_lower = processor.lower()

        # Azure FIRST. It resells OpenAI models under identical names, so
        # `gpt-4.1-mini` is ambiguous by model name alone and always resolves to
        # OpenAI — the processor is the only thing that can tell them apart.
        # Checked before the OpenAI branch because an Azure processor name may
        # also contain "gpt".
        if "azure" in processor_lower:
            return ServiceProviders.AZURE

        if "anthropic" in processor_lower or "claude" in processor_lower:
            return ServiceProviders.ANTHROPIC

        if any(keyword in processor_lower for keyword in ["google", "gemini"]):
            return ServiceProviders.GOOGLE

        if "cartesia" in processor_lower:
            return ServiceProviders.CARTESIA

        if "eleven" in processor_lower:
            return ServiceProviders.ELEVENLABS

        # OpenAI processors
        if any(keyword in processor_lower for keyword in ["openai", "gpt"]):
            return ServiceProviders.OPENAI

        # Groq processors
        if any(keyword in processor_lower for keyword in ["groq"]):
            return ServiceProviders.GROQ

        # Deepgram processors
        if any(keyword in processor_lower for keyword in ["deepgram"]):
            return ServiceProviders.DEEPGRAM

        # No default provider, for the same reason as _infer_provider_from_model:
        # guessing one silently prices usage against the wrong supplier's rates.
        return "unknown"

        return "unknown"

    def update_pricing(
        self, service_type: str, provider: str, model: str, pricing_model: PricingModel
    ):
        """Update pricing for a specific service/provider/model combination"""
        if service_type not in self.pricing_registry:
            self.pricing_registry[service_type] = {}
        if provider not in self.pricing_registry[service_type]:
            self.pricing_registry[service_type][provider] = {}
        self.pricing_registry[service_type][provider][model] = pricing_model


# Global cost calculator instance
cost_calculator = CostCalculator()
