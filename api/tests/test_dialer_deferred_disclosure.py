"""The recording notice plays into the rep's room once the prospect is in it, not
before the join (23 Oct: it added ~3 s of waiting to every answered call).

24 Oct: the automatic notice is off on outbound calls (the rep says it). These tests
switch it back on to keep its mechanics covered, and one checks it is off by default."""
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.services.telephony.dialer import extra_answer
from api.services.telephony.providers.twilio import routes
from api.services.telephony.providers.twilio.routes import router

_R = "api.services.telephony.providers.twilio.routes"
_NOTICE = "This call may be recorded and monitored for quality assurance."
PARENT = "CA" + "abcdef0123456789" * 2
WINNER = "CA" + "1" * 32
LEG = "11111111-1111-4111-8111-111111111111"
ENTRY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


import pytest


@pytest.fixture(autouse=True)
def _signed(monkeypatch):
    monkeypatch.setattr(routes, "_resolve_dialer_auth_token", AsyncMock(return_value="test-auth-token"))
    monkeypatch.setattr(extra_answer, "AUTO_RECORDING_NOTICE", True)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _join(client: TestClient, answered_by: str = "human"):
    return client.post(
        "/dialer-conference-join",
        params={
            "conference_name": f"call-{PARENT}", "parent_call_sid": PARENT,
            "parallel_leg_id": LEG, "parallel_entry_id": ENTRY,
            "events_url": "https://api.example.com/events",
            "recording_url": "https://api.example.com/recording",
        },
        data={"CallSid": WINNER, "To": "+15550000001", "AnsweredBy": answered_by},
        headers={"X-Twilio-Signature": "sig"},
    )


def _join_patches():
    return (
        patch(f"{_R}.RequestValidator.validate", return_value=True),
        patch(f"{_R}.dialer_parallel.claim_parallel_winner", return_value=True),
        patch(f"{_R}.dialer_parallel.parallel_legs", return_value=[{"child_call_sid": WINNER, "status": "answered"}]),
        patch(f"{_R}.cancel_call_if_ringing", return_value=True),
    )


def test_a_person_joins_the_rep_at_once_and_the_notice_is_owed(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "t")
    a, b, c, d = _join_patches()
    with a, b, c, d, patch(f"{_R}._defer_disclosure", AsyncMock(return_value=True)) as defer:
        response = _join(_client())
    assert response.status_code == 200
    assert "<Conference" in response.text
    assert _NOTICE not in response.text
    defer.assert_awaited_once_with(WINNER)


def test_the_notice_still_plays_first_when_it_cannot_be_deferred(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "t")
    a, b, c, d = _join_patches()
    with a, b, c, d, patch(f"{_R}._defer_disclosure", AsyncMock(return_value=False)):
        response = _join(_client())
    assert response.text.index(_NOTICE) < response.text.index("<Conference")


def test_a_mailbox_is_never_owed_the_notice(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "t")
    with patch(f"{_R}._defer_disclosure", AsyncMock(return_value=True)) as defer:
        with patch(f"{_R}.RequestValidator.validate", return_value=True):
            _join(_client(), answered_by="machine_end_beep")
    defer.assert_not_awaited()


def test_the_lead_joining_the_room_plays_the_owed_notice_once():
    with patch(f"{_R}.RequestValidator.validate", return_value=True), \
         patch(f"{_R}._take_deferred_disclosure", AsyncMock(side_effect=[True, False])) as take, \
         patch(f"{_R}._announce_disclosure", AsyncMock(return_value=True)) as announce, \
         patch(f"{_R}.update_dialer_call_conference_sid", AsyncMock()), \
         patch(f"{_R}.dialer_parallel.parallel_session", AsyncMock(return_value=None)), \
         patch(f"{_R}.record_overflow_join", AsyncMock(return_value=True)), \
         patch(f"{_R}.dialer_extra_answer.on_prospect_joined", AsyncMock()), \
         patch(f"{_R}.get_dialer_call_child_leg", AsyncMock(return_value=None)):
        client = _client()
        for _ in range(2):
            client.post("/dialer-conference-events", data={
                "StatusCallbackEvent": "participant-join", "ConferenceSid": "CF" + "9" * 32,
                "FriendlyName": f"call-{PARENT}", "CallSid": WINNER,
            }, headers={"X-Twilio-Signature": "sig"})
    assert take.await_count == 2
    announce.assert_awaited_once_with("CF" + "9" * 32)


def test_the_rep_joining_never_triggers_the_notice():
    with patch(f"{_R}.RequestValidator.validate", return_value=True), \
         patch(f"{_R}._take_deferred_disclosure", AsyncMock(return_value=True)) as take, \
         patch(f"{_R}.update_dialer_call_conference_sid", AsyncMock()), \
         patch(f"{_R}.dialer_parallel.parallel_session", AsyncMock(return_value=None)), \
         patch(f"{_R}.record_overflow_join", AsyncMock(return_value=True)), \
         patch(f"{_R}.dialer_extra_answer.on_prospect_joined", AsyncMock()), \
         patch(f"{_R}.get_dialer_call_child_leg", AsyncMock(return_value=None)):
        _client().post("/dialer-conference-events", data={
            "StatusCallbackEvent": "participant-join", "ConferenceSid": "CF" + "9" * 32,
            "FriendlyName": f"call-{PARENT}", "CallSid": PARENT,
        }, headers={"X-Twilio-Signature": "sig"})
    take.assert_not_awaited()


def test_the_disclosure_endpoint_says_the_notice(monkeypatch):
    with patch(f"{_R}.RequestValidator.validate", return_value=True):
        response = _client().post("/dialer-disclosure", data={}, headers={"X-Twilio-Signature": "sig"})
    assert f"<Say>{_NOTICE}</Say>" in response.text


def test_announce_asks_twilio_to_play_into_the_room():
    conf = MagicMock()
    twilio = MagicMock()
    twilio.conferences.return_value = conf
    with patch(f"{_R}._twilio_client", AsyncMock(return_value=twilio)), \
         patch(f"{_R}.get_backend_endpoints", AsyncMock(return_value=("https://api.example.com", "wss://x"))):
        import asyncio
        assert asyncio.run(routes._announce_disclosure("CF1")) is True
    conf.update.assert_called_once_with(
        announce_url="https://api.example.com/api/v1/telephony/dialer-disclosure", announce_method="POST",
    )



def test_by_default_a_person_hears_no_automatic_notice(monkeypatch):
    monkeypatch.setattr(extra_answer, "AUTO_RECORDING_NOTICE", False)
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "t")
    a, b, c, d = _join_patches()
    with a, b, c, d, patch(f"{_R}._defer_disclosure", AsyncMock(return_value=True)) as defer:
        response = _join(_client())
    assert response.status_code == 200
    assert "<Conference" in response.text
    assert _NOTICE not in response.text
    defer.assert_not_awaited()
