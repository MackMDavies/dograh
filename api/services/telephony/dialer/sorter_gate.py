"""F5: the call sorter's verdict, applied to a carrier AMD result before anything plays.

Provider-neutral (Twilio and SignalWire share it). Behind SYSEVO_DIALER_CALL_SORTER
(call_sorter_enabled): with the flag off ``consult_sorter`` returns the AMD result
unchanged without touching Redis, so every route behaves exactly as before.

Rules (BRIEF F5):
* AMD unsure (``unknown``): listen up to 4 s more. A sorter ``machine``/``beep``
  makes it a machine; anything else keeps today's unsure handling (a person path).
* AMD says machine: take a short veto window (2 s) before anything plays.
  - ``mailbox_full``  -> veto: nothing plays, the outcome is mailbox_full;
  - ``screener``      -> never a voicemail: handled like a person (the rep stays on,
                         a parallel line goes to the rep the way a human does);
  - ``person``        -> handled like a person too: the recording is never played
                         to a live person (decision 2).
  The sorter already reports a screener that gave up and offers a message
  ("screener_unavailable") as ``machine``, so that plays normally.
"""

from dataclasses import dataclass

from loguru import logger

from api.services.telephony.dialer import call_sorter

UNSURE_WAIT_SECONDS = 4.0
VETO_WINDOW_SECONDS = 2.0

_DECIDING_STAGES = ("person", "machine", "mailbox_full", "screener", "beep")


@dataclass(frozen=True)
class SorterDecision:
    """``kind`` is the effective AMD class: 'machine', 'person', 'unsure' or the
    input unchanged ('as_is'). ``mailbox_full`` is the veto. ``stage`` is what the
    sorter said (None: nothing / off)."""

    kind: str
    mailbox_full: bool = False
    stage: str | None = None


AS_IS = SorterDecision(kind="as_is")


async def consult_sorter(call_ref: str | None, *, amd: str) -> SorterDecision:
    """Combine an AMD class ('machine' | 'unsure') with the sorter for this call.

    Never raises; any failure (or the flag off) is ``AS_IS``.
    """
    if not call_ref or amd not in {"machine", "unsure"} or not call_sorter.call_sorter_enabled():
        return AS_IS
    timeout = UNSURE_WAIT_SECONDS if amd == "unsure" else VETO_WINDOW_SECONDS
    try:
        state = await call_sorter.wait_for_sorter_stage(
            call_ref, stages=_DECIDING_STAGES, timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - the sorter is optional, the call is not
        logger.warning(f"call sorter verdict unavailable for {call_ref}: {type(exc).__name__}")
        return AS_IS
    stage = (state or {}).get("stage")
    if stage == "mailbox_full":
        return SorterDecision(kind="machine", mailbox_full=True, stage=stage)
    if stage in {"machine", "beep"}:
        return SorterDecision(kind="machine", stage=stage)
    if stage in {"screener", "person"}:
        return SorterDecision(kind="person", stage=stage)
    return AS_IS
