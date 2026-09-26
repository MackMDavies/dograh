"""The rules behind Sam INBOUND hand-offs, free of I/O so they can be tested.

Two hand-offs between the reps' dialer and Sam INBOUND Sales (the inbound AI agent):

1. RING FIRST. A prospect calling a rep's number back rings the rep's dialer; if nobody
   picks up within SYSEVO_SAM_RING_FIRST_SECONDS, the caller is moved to Sam. Before,
   a free-but-away rep meant the caller waited on hold until they gave up (Solaris, One
   to One and Dado Technologies, 2026-09-22..25).

2. TRANSFER TO THE REP. A caller who asks Sam for a person is moved back into the room
   the rep's dialer joins, and the owning rep's dialer rings. Nobody answering within
   REP_ANSWER_SECONDS sends them back to Sam, who books a meeting.

Both move a LIVE SignalWire call with the Calling API's calling.transfer, pointed at one
of our own SWML webhooks (the way every dialer call already gets its instructions). A
move that fails leaves the call exactly where it was, which is what happened before.
"""

from __future__ import annotations

from typing import Any

#: How long the owning rep's dialer rings on a transfer before the caller goes back to Sam.
REP_ANSWER_SECONDS = 30

#: Only a hand-back to a rep this recent is honoured: Sam's call started well within it.
LIVE_CALL_WINDOW_MINUTES = 45


def ring_first_seconds(raw: str | None) -> int:
    """SYSEVO_SAM_RING_FIRST_SECONDS, or 0 (off). Bounded, so a typo cannot hang a caller
    on hold for an hour or bounce them to Sam before the rep's dialer has rung once."""
    try:
        value = int(str(raw or "").strip())
    except ValueError:
        return 0
    return value if 5 <= value <= 120 else 0


def phone_tail(number: str | None) -> str:
    """The last ten digits: how the dialer decides two numbers are the same line."""
    return "".join(ch for ch in str(number or "") if ch.isdigit())[-10:]


def calling_transfer_body(call_id: str, swml_url: str) -> dict[str, Any]:
    """The Calling API request that moves a live call onto the SWML at swml_url."""
    return {"command": "calling.transfer", "id": call_id, "params": {"dest": swml_url}}


def transfer_request_allowed(
    *,
    run_workflow_id: int | None,
    run_is_completed: bool | None,
    run_caller_number: str | None,
    claimed_caller_number: str | None,
    sam_workflow_id: int,
) -> tuple[bool, str]:
    """Whether Sam's transfer_to_rep request is genuine.

    The request carries the run id and caller number from Sam's own call context, and is
    honoured only if that run exists, belongs to Sam INBOUND, is still live, and is the
    call of the number it names. A forged request would need a live Sam call's run id AND
    that caller's number, and the worst it could do is ring a rep.
    """
    if run_workflow_id is None:
        return False, "no such call"
    if run_workflow_id != sam_workflow_id:
        return False, "not a Sam INBOUND call"
    if run_is_completed:
        return False, "that call has ended"
    tail = phone_tail(claimed_caller_number)
    if len(tail) < 10 or tail != phone_tail(run_caller_number):
        return False, "caller number does not match the call"
    return True, ""
