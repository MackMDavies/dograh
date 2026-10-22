"""The rep ends a single call while the lead still rings: the lead leg is cancelled
(21 Oct: a lead answered 1-2 minutes of silence after the rep had hung up)."""
from unittest.mock import AsyncMock, patch

import pytest

from api.services.telephony.providers.twilio import routes

_PATH = "api.services.telephony.providers.twilio.routes"
_PARENT = "CA" + "1" * 32
_CHILD = "CA" + "2" * 32

pytestmark = pytest.mark.no_rep_leg_stub


async def _run(*, ended, evidence=None, cancelled=True):
    with patch(f"{_PATH}._rep_watch_sleep", new=AsyncMock()), \
         patch(f"{_PATH}._rep_leg_ended", new=AsyncMock(side_effect=ended)) as rep, \
         patch(f"{_PATH}.get_voicemail_drop_evidence", new=AsyncMock(return_value=evidence or {})), \
         patch(f"{_PATH}.cancel_call_if_ringing", new=AsyncMock(return_value=cancelled)) as cancel:
        await routes._cancel_lead_if_rep_leaves(
            parent_call_sid=_PARENT, child_call_sid=_CHILD, ring_seconds=30,
        )
    return rep, cancel


@pytest.mark.asyncio
async def test_cancels_the_ringing_lead_once_the_rep_leg_ends():
    rep, cancel = await _run(ended=[False, False, True])
    assert rep.await_count == 3
    cancel.assert_awaited_once_with(call_sid=_CHILD)


@pytest.mark.asyncio
async def test_leaves_the_lead_ringing_for_a_queued_voicemail():
    _, cancel = await _run(ended=[True], evidence={"rep_detached_at": "2026-10-22T00:00:00Z"})
    cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_stops_watching_after_the_ring_window_while_the_rep_stays():
    rep, cancel = await _run(ended=lambda *_: False)
    cancel.assert_not_awaited()
    assert rep.await_count == 15  # (30 s ring + 15 s) / 3 s
