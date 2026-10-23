"""Contextual delivery instructions shared by realtime and cascaded agents."""

from typing import Any

TAG_MODELS = {"eleven_v3", "eleven_v3_conversational", "eleven_v4", "eleven_v4_turbo"}
EMOTIONS = {
    "warm": "warm and welcoming",
    "calm": "calm and reassuring",
    "patient": "patient, with space for the caller to finish",
    "confident": "confident without overstating certainty",
    "enthusiastic": "enthusiastic when the context warrants it",
    "curious": "interested and curious without prying",
    "professional": "professional and clear",
}
TAGS = {"laughs", "chuckles", "sighs", "whispers", "slow", "excited", "curious", "hesitant"}


def build_voice_expression_instruction(tts, realtime=None, value: Any = None) -> str | None:
    if value is None and realtime is None:
        from api.services.configuration.voice_settings import build_expressive_mode_instruction
        return build_expressive_mode_instruction(tts)
    config = value if isinstance(value, dict) else {}
    legacy = getattr(tts, "expressive_mode", None)
    model = getattr(tts, "model", "")
    enabled = config.get("enabled", (legacy if legacy is not None else model == "eleven_v4_turbo") if realtime is None else False)
    if not enabled:
        return None
    emotions = config.get("emotions", ["warm", "calm", "patient"])
    emotions = emotions if isinstance(emotions, list) else []
    selected = [EMOTIONS[key] for key in dict.fromkeys(e for e in emotions if isinstance(e, str)) if key in EMOTIONS]
    empathy = config.get("empathy", "balanced")
    lines = ["# Voice and empathy", "Adapt delivery to the conversation; do not announce emotion labels or repeat stock empathy phrases."]
    if selected:
        lines.append("Preferred delivery: " + "; ".join(selected) + ".")
    if empathy == "supportive":
        lines.append("When the caller expresses distress or frustration, briefly acknowledge their specific concern, use a reassuring tone, and offer a concrete next step. Do not claim to feel their emotions.")
    elif empathy == "restrained":
        lines.append("Keep emotional acknowledgement brief and factual. Remain courteous, direct, and practical.")
    else:
        lines.append("Acknowledge the caller's concern naturally when relevant, then help. Match warmth to context without exaggerated reassurance.")
    if config.get("adapt_to_context", True):
        lines.extend([
            "For frustration, complaints, or worry: slow down, remain composed, and avoid cheerful or amused delivery.",
            "For good news: respond with appropriate warmth; for instructions or uncertainty: use clear, measured delivery and state uncertainty honestly.",
            "Never let a tone instruction override the agent's policies, factual accuracy, tool permissions, or guardrails.",
        ])
    custom = config.get("instructions")
    if isinstance(custom, str) and custom.strip():
        lines.append("Additional delivery guidance: " + custom.strip()[:2000])
    provider = getattr(tts, "provider", None)
    if realtime is None and provider == "elevenlabs" and model in TAG_MODELS:
        tags = config.get("audio_tags", getattr(tts, "suggested_audio_tags", []) or [])
        tags = tags if isinstance(tags, list) else []
        tags = [t for t in dict.fromkeys(t for t in tags if isinstance(t, str)) if t in TAGS]
        if tags:
            lines.append("Use these supported audio tags sparingly when appropriate: " + ", ".join(f"[{t}]" for t in tags) + ". Never prefix every response with a tag or read tag names aloud.")
    else:
        lines.append("Express tone through natural wording and vocal delivery. Do not emit bracketed audio tags, stage directions, or emotion labels.")
    return "\n".join(lines)
