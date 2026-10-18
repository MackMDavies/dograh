from unittest.mock import AsyncMock, patch

import pytest

from api.services.telephony.dialer import signalwire_parallel_store


async def test_screened_single_start_calls_service_rpc_for_exactly_one_entry():
    row = {
        "id": "00000000-0000-0000-0000-000000000003",
        "entry_id": "00000000-0000-0000-0000-000000000004",
    }
    with patch.object(signalwire_parallel_store, "_rpc", new=AsyncMock(return_value=[row])) as rpc:
        result = await signalwire_parallel_store.start_screened_single_session(
            session_id="00000000-0000-0000-0000-000000000001",
            rep_user_id="00000000-0000-0000-0000-000000000002",
            rep_call_id="browser-call-0001",
            conference_name="sysevo-par-00000000-0000-0000-0000-000000000001",
            entry_id="00000000-0000-0000-0000-000000000004",
        )

    assert result == [row]
    rpc.assert_awaited_once_with(
        "dialer_signalwire_screened_single_start_v2",
        {
            "p_session_id": "00000000-0000-0000-0000-000000000001",
            "p_rep_user_id": "00000000-0000-0000-0000-000000000002",
            "p_rep_call_id": "browser-call-0001",
            "p_conference_name": "sysevo-par-00000000-0000-0000-0000-000000000001",
            "p_entry_id": "00000000-0000-0000-0000-000000000004",
        },
    )


@pytest.mark.parametrize("result", [None, [], [{"id": "one"}, {"id": "two"}], [None]])
async def test_screened_single_start_rejects_uncertain_reservation(result):
    with patch.object(signalwire_parallel_store, "_rpc", new=AsyncMock(return_value=result)):
        with pytest.raises(signalwire_parallel_store.SignalWireParallelStoreError):
            await signalwire_parallel_store.start_screened_single_session(
                session_id="00000000-0000-0000-0000-000000000001",
                rep_user_id="00000000-0000-0000-0000-000000000002",
                rep_call_id="browser-call-0001",
                conference_name="sysevo-par-00000000-0000-0000-0000-000000000001",
                entry_id="00000000-0000-0000-0000-000000000004",
            )


async def test_parallel_reservations_are_mapped_to_their_selected_leads():
    rows = [{"id": "leg-b", "entry_id": "entry-b"}, {"id": "leg-a", "entry_id": "entry-a"}]
    with patch.object(signalwire_parallel_store, "_rpc", new=AsyncMock(return_value=rows)) as rpc:
        result = await signalwire_parallel_store.start_session(
            session_id="session", rep_user_id="rep", rep_call_id="browser-call",
            conference_name="sysevo-par-session", entry_ids=["entry-a", "entry-b"], line_count=2,
        )
    assert [row["id"] for row in result] == ["leg-a", "leg-b"]
    assert rpc.await_args.args[0] == "dialer_signalwire_parallel_start_v2"


@pytest.mark.parametrize("rows", [
    [{"id": "leg-a", "entry_id": "entry-a"}, {"id": "leg-b", "entry_id": "other-lead"}],
    [{"id": "leg-a", "entry_id": "entry-a"}, {"id": "leg-b", "entry_id": "entry-a"}],
    [{"id": "same-leg", "entry_id": "entry-a"}, {"id": "same-leg", "entry_id": "entry-b"}],
])
async def test_parallel_rejects_mismatched_duplicate_or_reused_lead_reservations(rows):
    with patch.object(signalwire_parallel_store, "_rpc", new=AsyncMock(return_value=rows)):
        with pytest.raises(signalwire_parallel_store.SignalWireParallelStoreError):
            await signalwire_parallel_store.start_session(
                session_id="session", rep_user_id="rep", rep_call_id="browser-call",
                conference_name="sysevo-par-session", entry_ids=["entry-a", "entry-b"], line_count=2,
            )
