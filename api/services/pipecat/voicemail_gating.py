"""Decides whether a call is one that can plausibly reach a voicemail system.

Voicemail detection is not free. The classifier sits in front of the main LLM
(`VoicemailDetector.llm_gate()`), so the agent's first reply waits on a full
extra completion before the conversation LLM is even asked for one. That cost
lands on the first turn of the call — the turn where a caller is most likely to
hang up — so it should only be paid on calls where an answering machine can
actually pick up.
"""

from api.enums import CallType


def should_run_voicemail_detection(
    voicemail_config: dict | None,
    *,
    is_realtime: bool,
    is_telephony: bool,
    call_type: str | None,
) -> bool:
    """Return True when voicemail detection should be wired into the pipeline.

    Args:
        voicemail_config: The workflow's ``voicemail_detection`` configuration.
        is_realtime: Whether the run uses a speech-to-speech model. The
            realtime pipeline has no separate LLM stage to gate.
        is_telephony: Whether the call is carried over a telephony provider.
            Browser/WebRTC calls (the agent test panel, the embedded widget)
            are answered by a person pressing a button — there is no
            answering machine on the other end.
        call_type: ``"inbound"`` or ``"outbound"``. On an inbound call a human
            dialled us, so there is nothing to classify.
    """
    if not (voicemail_config or {}).get("enabled", False):
        return False

    if is_realtime:
        return False

    if not is_telephony:
        return False

    return call_type != CallType.INBOUND.value
