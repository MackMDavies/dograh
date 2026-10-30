"""Beep-timed voicemail trial: the sorter's beep starts the recording for trial reps."""
from unittest.mock import AsyncMock, patch

import pytest

from api.services.telephony.dialer import call_sorter
from api.services.telephony.providers.twilio import routes

_PATH = "api.services.telephony.providers.twilio.routes"
_PARENT = "CA" + "1" * 32
_CHILD = "CA" + "2" * 32
_REP = "94bec25b-e517-40ea-829e-b30a7ae2fdb4"
_URL = "https://example.test/vm.wav"

pytestmark = pytest.mark.no_rep_leg_stub


def _row(**over):
    row = {
        "rep_user_id": _REP, "provider": "twilio", "child_call_sid": _CHILD,
        "ended_at": None, "voicemail_drop_started_at": None, "auto_voicemail_sent": False,
        "manual_voicemail_requested_at": None, "lead_joined_at": None,
    }
    return {**row, **over}


async def _watch(*, row, stage="beep", trial=True, enabled=False):
    mocks = {}
    with patch(f"{_PATH}.call_sorter.beep_trial_user", return_value=trial), \
         patch(f"{_PATH}.call_sorter.call_sorter_enabled", return_value=False), \
         patch(f"{_PATH}.get_backend_endpoints", new=AsyncMock(return_value=("https://api.test", None))), \
         patch(f"{_PATH}._twilio_client", new=AsyncMock(return_value=object())), \
         patch(f"{_PATH}._resolve_dialer_auth_token", new=AsyncMock(return_value="s")), \
         patch(f"{_PATH}.dialer_call_sorter.start_prospect_stream", new=AsyncMock(return_value="MZ1")) as start, \
         patch(f"{_PATH}.call_sorter.wait_for_sorter_stage", new=AsyncMock(return_value={"stage": stage} if stage else None)), \
         patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value=row)), \
         patch(f"{_PATH}.get_auto_voicemail_settings", new=AsyncMock(return_value=(enabled, _URL, "drop-1"))), \
         patch(f"{_PATH}.get_team_voicemail_policy", new=AsyncMock(return_value={"on_machine": "drop_vm"})), \
         patch(f"{_PATH}.update_dialer_call_amd_result", new=AsyncMock(return_value=True)) as amd, \
         patch(f"{_PATH}.claim_manual_voicemail_playback", new=AsyncMock(return_value=True)) as claim, \
         patch(f"{_PATH}.attach_voicemail_drop_to_call", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}._redirect_twilio_child_to_voicemail", new=AsyncMock(return_value=True)) as redirect, \
         patch(f"{_PATH}.release_manual_voicemail_playback", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.call_stages.record_call_stage", new=AsyncMock()), \
         patch(f"{_PATH}._release_rep_for_voicemail", new=AsyncMock()) as release:
        result = await routes._beep_trial_watch(parent_call_sid=_PARENT, child_call_sid=_CHILD)
        mocks.update(start=start, amd=amd, claim=claim, redirect=redirect, release=release)
    return result, mocks


@pytest.mark.asyncio
async def test_queued_send_voicemail_plays_on_the_beep_with_no_pause():
    result, m = await _watch(row=_row(manual_voicemail_requested_at="2026-10-22T00:00:00Z"))
    assert result == "played"
    m["start"].assert_awaited_once()
    assert m["start"].await_args.kwargs["force"] is True
    m["amd"].assert_awaited_once_with(parent_call_sid=_PARENT, answered_by="machine_end_beep")
    m["claim"].assert_awaited_once_with(parent_call_sid=_PARENT, child_call_sid=_CHILD, manual=True)
    twiml = m["redirect"].await_args.kwargs["twiml"]
    assert "<Play>" in twiml and "<Pause" not in twiml
    m["release"].assert_not_awaited()  # a queued send already freed the rep


@pytest.mark.asyncio
async def test_automatic_mode_plays_on_the_beep_and_frees_the_rep():
    result, m = await _watch(row=_row(), enabled=True)
    assert result == "played"
    m["claim"].assert_awaited_once_with(parent_call_sid=_PARENT, child_call_sid=_CHILD, manual=False)
    m["release"].assert_awaited_once_with(_PARENT)


@pytest.mark.asyncio
async def test_ask_mode_without_a_request_leaves_the_mailbox_to_the_rep():
    result, m = await _watch(row=_row(lead_joined_at="2026-10-22T00:00:01Z"), enabled=False)
    assert result == "not_requested"
    m["redirect"].assert_not_awaited()


@pytest.mark.asyncio
async def test_a_person_is_never_played_to():
    result, m = await _watch(row=_row(manual_voicemail_requested_at="x"), stage="person")
    assert result == "person"
    m["claim"].assert_not_awaited()
    m["redirect"].assert_not_awaited()


@pytest.mark.asyncio
async def test_other_reps_are_untouched():
    result, m = await _watch(row=_row(), trial=False)
    assert result == "not_trial"
    m["start"].assert_not_awaited()


def test_trial_users_come_from_the_env(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "k")
    monkeypatch.setenv("SYSEVO_DIALER_BEEP_TRIAL_USERS", f" {_REP} , other ")
    assert call_sorter.beep_trial_user(_REP) is True
    assert call_sorter.beep_trial_user("someone-else") is False
    monkeypatch.delenv("DEEPGRAM_API_KEY")
    assert call_sorter.beep_trial_user(_REP) is False
