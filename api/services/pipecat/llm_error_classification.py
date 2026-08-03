"""Provider-agnostic detection of LLM rate-limit / quota-exhaustion errors.

Every pipecat LLM service (OpenAI, Anthropic, Google, Groq, ...) eventually
funnels a failed completion call into a generic ``except Exception as e:
await self.push_error(..., exception=e)`` — the exception shape differs per
provider SDK, but rate-limit/quota errors consistently surface an HTTP-429-ish
status somewhere on the exception (``status_code``, ``code``, or ``status``)
and/or a recognizable phrase in the message (this mirrors the same
best-effort, duck-typed attribute probing already used in
``realtime_feedback_observer.py`` to surface structured error fields to the
UI). Used to tag this *specific* failure mode distinctly from a generic
pipeline error, so operators aren't left guessing why a call went dead.
"""

_QUOTA_KEYWORDS = (
    "rate limit",
    "rate_limit",
    "quota",
    "resource_exhausted",
    "insufficient_quota",
    "too many requests",
)


def classify_llm_exhaustion(exc: Exception | None) -> dict | None:
    """Return a small detail dict if ``exc`` looks like a rate-limit/quota
    error, else None. Never raises."""
    if exc is None:
        return None

    try:
        for attr in ("status_code", "code", "status"):
            value = getattr(exc, attr, None)
            if value in (429, "429"):
                return {
                    "reason": "rate_limited_or_quota_exceeded",
                    "matched_on": attr,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc)[:500],
                }

        message = str(exc).lower()
        if any(keyword in message for keyword in _QUOTA_KEYWORDS):
            return {
                "reason": "rate_limited_or_quota_exceeded",
                "matched_on": "message",
                "exception_type": type(exc).__name__,
                "exception_message": str(exc)[:500],
            }
    except Exception:
        # Classification is best-effort diagnostics — never let it break the
        # actual error-handling path it feeds into.
        return None

    return None
