"""Short spoken lines for when the LLM stalls mid-call.

When the model provider rate-limits or times out, the pipeline has nothing to
say and the caller hears silence. In production that read as a dropped call:
"Hello? … Hello?" then a hangup.

A brief, human line holds the conversation open while the pipeline recovers.
It is deliberately capped — if the provider is genuinely down, stringing the
caller along with filler is worse than letting the call end.

Lines avoid the agent's banned conversational openers so a stall does not
sound like a different persona suddenly took over.
"""

from typing import Optional

# Ordered so consecutive stalls sound like a person thinking, not a loop.
RECOVERY_LINES: tuple[str, ...] = (
    "Bear with me one second.",
    "Sorry — just pulling that up now.",
    "One moment, still with you.",
)

MAX_RECOVERY_LINES_PER_CALL = len(RECOVERY_LINES)


def pick_recovery_line(attempt: int) -> Optional[str]:
    """Return the line to speak for a given stall, or None once exhausted.

    Args:
        attempt: Zero-based count of stalls already handled on this call.
    """
    if attempt >= MAX_RECOVERY_LINES_PER_CALL:
        return None
    return RECOVERY_LINES[max(attempt, 0)]
