"""Concurrency and partial-failure behavior for real parallel call orchestration."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from api.services.telephony.dialer.signalwire_routes import (
    _cancel_signalwire_ringing_siblings,
    _originate_signalwire_batch,
)

MODULE = "api.services.telephony.dialer.signalwire_routes"
CALLING = "api.services.telephony.dialer.signalwire_calling"


def batch_inputs(count):
    entries = [{"id": f"entry-{i}", "phone": f"+14155550{i:03d}"} for i in range(count)]
    return dict(
        session_id="session", rep_user_id="rep", entries=entries,
        reserved_legs=[{"id": f"leg-{i}"} for i in range(count)],
        caller_ids={entry["id"]: "+14155550999" for entry in entries},
        backend_endpoint="https://api.example.test",
    )


@pytest.mark.parametrize("count", [2, 3, 5, 10])
async def test_all_selected_lines_originate_before_any_provider_response(count):
    """A serial implementation deadlocks at the provider barrier and fails."""
    started = []
    all_started = asyncio.Event()

    async def dial(**kwargs):
        started.append(kwargs["to_number"])
        if len(started) == count:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=1)
        return {"id": "call-" + kwargs["to_number"]}

    with (
        patch(f"{CALLING}.originate_call", new=AsyncMock(side_effect=dial)),
        patch(f"{MODULE}.signalwire_parallel_store.register_provider_call", new=AsyncMock(return_value=True)),
        patch(f"{MODULE}.create_dialer_call", new=AsyncMock(return_value=True)) as history,
    ):
        await _originate_signalwire_batch(**batch_inputs(count))

    assert len(set(started)) == count
    assert history.await_count == count


@pytest.mark.parametrize("failure", ["originate", "registration", "history"])
async def test_partial_failure_cancels_even_a_late_successful_originate(failure):
    failure_seen = asyncio.Event()
    inputs = batch_inputs(3)
    bad_number = inputs["entries"][0]["phone"]

    async def dial(**kwargs):
        if kwargs["to_number"] == bad_number and failure == "originate":
            failure_seen.set()
            raise RuntimeError("provider rejected line")
        if kwargs["to_number"] != bad_number:
            await asyncio.wait_for(failure_seen.wait(), timeout=1)
        return {"id": "call-" + kwargs["to_number"]}

    async def register(**kwargs):
        if kwargs["leg_id"] == "leg-0" and failure == "registration":
            failure_seen.set()
            return False
        return True

    async def history(**kwargs):
        if kwargs["to_number"] == bad_number and failure == "history":
            failure_seen.set()
            return False
        return True

    with (
        patch(f"{CALLING}.originate_call", new=AsyncMock(side_effect=dial)),
        patch(f"{CALLING}.end_call", new=AsyncMock()) as end,
        patch(f"{MODULE}.signalwire_parallel_store.register_provider_call", new=AsyncMock(side_effect=register)),
        patch(f"{MODULE}.create_dialer_call", new=AsyncMock(side_effect=history)),
        patch(f"{MODULE}.signalwire_parallel_store.update_leg", new=AsyncMock(return_value=True)),
        patch(f"{MODULE}.update_dialer_call_status", new=AsyncMock()),
        patch(f"{MODULE}.signalwire_parallel_store.finish_session", new=AsyncMock(return_value=True)) as finish,
    ):
        with pytest.raises(RuntimeError, match="complete parallel batch"):
            await _originate_signalwire_batch(**inputs)

    expected = {"call-" + entry["phone"] for entry in inputs["entries"]}
    if failure == "originate":
        expected.remove("call-" + bad_number)
    assert {call.kwargs["call_id"] for call in end.await_args_list} == expected
    finish.assert_awaited_once_with(session_id="session", status="failed")


async def test_sibling_that_answered_during_cleanup_is_never_ended():
    with (
        patch(f"{MODULE}.signalwire_parallel_store.get_session_legs", new=AsyncMock(return_value=[
            {"id": "stale", "provider_call_id": "call-answered", "status": "ringing"},
            {"id": "ringing", "provider_call_id": "call-ringing", "status": "ringing"},
        ])),
        patch(f"{MODULE}.signalwire_parallel_store.claim_ringing_cancel", new=AsyncMock(side_effect=[False, True])),
        patch(f"{CALLING}.end_call", new=AsyncMock()) as end,
        patch(f"{MODULE}.signalwire_parallel_store.record_outcome", new=AsyncMock()),
        patch(f"{MODULE}.update_dialer_call_status", new=AsyncMock()),
    ):
        await _cancel_signalwire_ringing_siblings(
            session_id="session", winner_leg_id="winner", rep_user_id="rep",
        )

    end.assert_awaited_once_with(call_id="call-ringing", reason="noAnswer")
