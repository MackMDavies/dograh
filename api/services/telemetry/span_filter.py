"""Apply an org's Langfuse "What to send" options to a span before export.

Pure functions over (span name, attributes) so they are testable without OTel.

* traces       – the call trace skeleton: ``conversation`` and ``turn-N`` spans.
* spans        – model spans: LLM / STT / TTS (anything with ``gen_ai.*``).
* tools        – tool activity: knowledge-base retrieval and tool/function spans, plus
                 the ``tools.*`` / ``function_calls.*`` attributes on LLM spans.
* transcripts  – the words spoken: ``transcript*`` attributes and the input/output
                 text of STT/TTS spans. Off strips them; the spans themselves stay.
* redact       – replaces phone numbers, email addresses and card numbers in every
                 string attribute with ``[phone]`` / ``[email]`` / ``[card]``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 13–19 digits with optional spaces/dashes (payment cards), checked before phones.
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
# E.164 / national formats: 7+ digits with optional +, spaces, dashes, dots, parens.
_PHONE = re.compile(r"(?<!\w)\+?\(?\d[\d\s().-]{6,}\d(?!\w)")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:$|[T\s])")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def redact_text(value: str) -> str:
    value = _EMAIL.sub("[email]", value)

    def card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return "[card]" if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)

    value = _CARD.sub(card, value)

    def phone(m: re.Match) -> str:
        if _DATE.match(m.group(0).strip()):
            return m.group(0)  # ISO dates / timestamps are not phone numbers
        digits = re.sub(r"\D", "", m.group(0))
        return "[phone]" if 7 <= len(digits) <= 15 else m.group(0)

    return _PHONE.sub(phone, value)


def span_kind(name: str, attributes: Mapping[str, Any]) -> str:
    """'trace' | 'tool' | 'model' | 'other'."""
    if name == "conversation" or name.startswith("turn-") or name == "turn":
        return "trace"
    low = name.lower()
    if "tool" in low or "function" in low or low == "knowledge_base_retrieval":
        return "tool"
    if name in ("llm", "stt", "tts") or any(k.startswith("gen_ai.") for k in attributes):
        return "model"
    return "other"


_TRANSCRIPT_TEXT_KINDS = ("stt", "tts")


def filter_span(
    name: str, attributes: Mapping[str, Any], options: Mapping[str, bool]
) -> Optional[Dict[str, Any]]:
    """The attributes to export for this span, or None to drop the span."""
    kind = span_kind(name, attributes)
    if kind == "trace" and not options.get("traces", True):
        return None
    if kind == "model" and not options.get("spans", True):
        return None
    if kind == "tool" and not options.get("tools", True):
        return None

    out: Dict[str, Any] = {}
    speech = name in _TRANSCRIPT_TEXT_KINDS or attributes.get("gen_ai.output.type") == "speech"
    for k, v in attributes.items():
        if not options.get("tools", True) and (k.startswith("tools.") or k.startswith("function_calls.")):
            continue
        if not options.get("transcripts", False):
            if k == "transcript" or k.startswith("transcript."):
                continue
            if speech and k in ("input", "output"):
                continue
        if options.get("redact", False) and isinstance(v, str):
            v = redact_text(v)
        out[k] = v
    return out
