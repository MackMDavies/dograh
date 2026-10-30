"""In-call controls for a rep's own Twilio dialer conference."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.services.auth.sysevo_roles import require_sales_dialer_role
from api.services.telephony.providers.twilio import dialer_call_controls as cc

REP = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
TEAMMATE = "33333333-3333-3333-3333-333333333333"
PARENT = "CA" + "a" * 32
CHILD = "CA" + "b" * 32
EXTRA = "CA" + "c" * 32
CONF = "CF" + "d" * 32
REC = "RE" + "e" * 32


class FakeParticipants:
    def __init__(self, conf):
        self.conf = conf

    def list(self):
        return list(self.conf.participants.values())

    def create(self, **kw):
        self.conf.log.append(("create", kw))
        sid = EXTRA
        self.conf.participants[sid] = SimpleNamespace(call_sid=sid, hold=False, status="ringing")
        return SimpleNamespace(call_sid=sid)

    def __call__(self, call_sid):
        conf = self.conf

        class One:
            def update(self, **kw):
                conf.log.append(("update", call_sid, kw))
                p = conf.participants.get(call_sid)
                if p is not None and "hold" in kw:
                    p.hold = kw["hold"]

            def delete(self):
                conf.log.append(("delete", call_sid))
                conf.participants.pop(call_sid, None)

        return One()


class FakeRecordings:
    def __init__(self, conf):
        self.conf = conf

    def list(self, limit=5):
        return list(self.conf.recordings)

    def __call__(self, sid):
        conf = self.conf

        class One:
            def update(self, **kw):
                conf.log.append(("recording", sid, kw))

        return One()


class FakeConference:
    def __init__(self, log, participants, recordings):
        self.log = log
        self.participants = participants
        self.recordings = recordings


class FakeTwilio:
    def __init__(self, participants=None, recordings=None):
        self.log = []
        self.conf = FakeConference(
            self.log,
            participants if participants is not None else {
                PARENT: SimpleNamespace(call_sid=PARENT, hold=False, status="connected"),
                CHILD: SimpleNamespace(call_sid=CHILD, hold=False, status="connected"),
            },
            recordings if recordings is not None else [SimpleNamespace(sid=REC, status="in-progress")],
        )
        outer = self

        class Conferences:
            def list(self, **kw):
                return [SimpleNamespace(sid=CONF)]

            def __call__(self, sid):
                assert sid == CONF
                return SimpleNamespace(
                    participants=FakeParticipants(outer.conf),
                    recordings=FakeRecordings(outer.conf),
                )

        self.conferences = Conferences()


def _row(**over):
    row = {
        "id": "row-1", "parent_call_sid": PARENT, "child_call_sid": CHILD, "status": "in-progress",
        "rep_user_id": REP, "provider": "twilio", "conference_sid": CONF,
        "lead_joined_at": "2027-04-21T10:00:00Z", "ended_at": None,
        "from_number": "+16025550100", "to_number": "+16028212243", "entry_id": None,
    }
    row.update(over)
    return row


@pytest.fixture
def env(monkeypatch):
    state = SimpleNamespace(row=_row(), events=[], twilio=FakeTwilio(), spawned=[], rpc_calls=[])

    async def call_row(parent):
        return state.row if parent == state.row["parent_call_sid"] else None

    async def events(parent):
        return list(state.events)

    async def insert_event(row):
        saved = {"id": f"00000000-0000-4000-8000-{len(state.events):012d}", **row}
        state.events.append(saved)
        return saved

    async def update_event(event_id, patch):
        for e in state.events:
            if e["id"] == event_id:
                e.update(patch)

    async def rpc(name, body):
        state.rpc_calls.append((name, body))
        return [{"inbound_call_id": "inv-1", "target_name": "Daniel", "reason": None}]

    def spawn(coro):
        state.spawned.append(coro)

    monkeypatch.setattr(cc, "_call_row", call_row)
    monkeypatch.setattr(cc, "_events", events)
    monkeypatch.setattr(cc, "_insert_event", insert_event)
    monkeypatch.setattr(cc, "_update_event", update_event)
    monkeypatch.setattr(cc, "_rpc", rpc)
    monkeypatch.setattr(cc, "_twilio_client", AsyncMock(side_effect=lambda: state.twilio))
    monkeypatch.setattr(cc, "_backend_endpoint", AsyncMock(return_value="https://api.example.com"))
    monkeypatch.setattr(cc, "_number_block_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(cc, "_expire_offer", AsyncMock(return_value=True))
    monkeypatch.setattr(cc, "_offer_row", AsyncMock(return_value=None))
    monkeypatch.setattr(cc, "_spawn", spawn)
    yield state
    for coro in state.spawned:
        coro.close()


def _client(user_id=REP):
    app = FastAPI()
    app.include_router(cc.router)
    app.dependency_overrides[require_sales_dialer_role] = lambda: SimpleNamespace(provider_id=user_id)
    return TestClient(app)


# ── Ownership and providers ──────────────────────────────────────────────────────

def test_another_reps_call_is_refused_without_touching_twilio(env):
    client = _client(OTHER)
    assert client.get(f"/dialer/call-control/{PARENT}").status_code == 404
    r = client.post("/dialer/call-control", json={"parent_call_sid": PARENT, "action": "hold"})
    assert r.status_code == 404
    assert env.twilio.log == []


def test_signalwire_call_reports_capabilities_false_with_reasons(env):
    env.row = _row(provider="signalwire", parent_call_sid="sw-call-1")
    r = _client().get("/dialer/call-control/sw-call-1")
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "signalwire"
    assert not any(body["capabilities"].values())
    assert "SignalWire" in body["reasons"]["hold"]
    r = _client().post("/dialer/call-control", json={"parent_call_sid": "sw-call-1", "action": "hold"})
    assert r.status_code == 409


def test_ended_call_cannot_be_controlled(env):
    env.row = _row(ended_at="2027-04-21T10:05:00Z")
    r = _client().post("/dialer/call-control", json={"parent_call_sid": PARENT, "action": "hold"})
    assert r.status_code == 409


# ── State ────────────────────────────────────────────────────────────────────────

def test_state_reports_participants_recording_and_consent(env):
    body = _client().get(f"/dialer/call-control/{PARENT}").json()
    assert body["provider"] == "twilio"
    assert body["recording"] == "in-progress"
    assert body["consent_played"] is True
    assert body["on_hold"] is False
    kinds = {p["kind"]: p for p in body["participants"]}
    assert kinds["rep"]["id"] == PARENT and kinds["prospect"]["id"] == CHILD
    assert body["capabilities"]["hold"] and body["capabilities"]["transfer"] and body["capabilities"]["add_call"]
    assert body["capabilities"]["transfer_agent"] is False and body["reasons"]["transfer_agent"]
    assert body["max_participants"] == 5


def test_consent_is_not_claimed_before_the_prospect_joined(env):
    env.row = _row(lead_joined_at=None)
    env.twilio = FakeTwilio(participants={PARENT: SimpleNamespace(call_sid=PARENT, hold=False, status="connected")})
    body = _client().get(f"/dialer/call-control/{PARENT}").json()
    assert body["consent_played"] is False
    assert body["capabilities"]["hold"] is False
    assert "answer" in body["reasons"]["hold"]


def test_missing_recording_is_absent_not_paused(env):
    env.twilio = FakeTwilio(recordings=[])
    body = _client().get(f"/dialer/call-control/{PARENT}").json()
    assert body["recording"] == "absent"
    assert body["capabilities"]["record"] is False


# ── Hold / record / DTMF ─────────────────────────────────────────────────────────

def test_hold_and_unhold_target_the_prospect_participant(env):
    client = _client()
    r = client.post("/dialer/call-control", json={"parent_call_sid": PARENT, "action": "hold"})
    assert r.status_code == 200 and r.json()["on_hold"] is True
    op = env.twilio.log[0]
    assert op[0] == "update" and op[1] == CHILD and op[2]["hold"] is True and op[2]["hold_url"]
    r = client.post("/dialer/call-control", json={"parent_call_sid": PARENT, "action": "unhold"})
    assert r.json()["on_hold"] is False
    assert env.twilio.log[-1] == ("update", CHILD, {"hold": False})
    assert [e["action"] for e in env.events] == ["hold", "unhold"]


def test_record_pause_and_resume_update_the_conference_recording(env):
    client = _client()
    client.post("/dialer/call-control", json={"parent_call_sid": PARENT, "action": "record_pause"})
    assert env.twilio.log[-1] == ("recording", REC, {"status": "paused", "pause_behavior": "skip"})
    env.twilio.conf.recordings[0].status = "paused"
    client.post("/dialer/call-control", json={"parent_call_sid": PARENT, "action": "record_resume"})
    assert env.twilio.log[-1] == ("recording", REC, {"status": "in-progress"})


def test_record_without_a_recording_is_refused(env):
    env.twilio = FakeTwilio(recordings=[])
    r = _client().post("/dialer/call-control", json={"parent_call_sid": PARENT, "action": "record_pause"})
    assert r.status_code == 409


def test_dtmf_announces_digits_on_the_prospect_leg(env):
    r = _client().post("/dialer/call-control", json={"parent_call_sid": PARENT, "action": "dtmf", "digits": "1#"})
    assert r.status_code == 200
    op = env.twilio.log[0]
    assert op[0] == "update" and op[1] == CHILD
    assert op[2]["announce_url"].startswith("https://api.example.com/api/v1/telephony/dialer/call-control/dtmf-twiml?digits=1%23")


def test_dtmf_rejects_bad_digits(env):
    r = _client().post("/dialer/call-control", json={"parent_call_sid": PARENT, "action": "dtmf", "digits": "1<x>"})
    assert r.status_code == 400
    assert env.twilio.log == []


def test_dtmf_twiml_requires_twilio_signature(monkeypatch):
    from api.services.telephony.providers.twilio import routes

    app = FastAPI()
    app.include_router(cc.router)
    monkeypatch.setattr(routes, "_verify_twilio_signature", AsyncMock(return_value=False))
    r = TestClient(app).post("/dialer/call-control/dtmf-twiml?digits=12", data={})
    assert "<Play" not in r.text
    monkeypatch.setattr(routes, "_verify_twilio_signature", AsyncMock(return_value=True))
    r = TestClient(app).post("/dialer/call-control/dtmf-twiml?digits=12", data={})
    assert '<Play digits="12"/>' in r.text


# ── Add call / cap ───────────────────────────────────────────────────────────────

def test_add_number_dials_into_the_conference_from_the_calls_caller_id(env):
    r = _client().post("/dialer/call-control/participants", json={
        "parent_call_sid": PARENT, "target": {"type": "number", "phone": "+14155550123"},
    })
    assert r.status_code == 200
    kind, kw = env.twilio.log[0]
    assert kind == "create" and kw["to"] == "+14155550123" and kw["from_"] == "+16025550100"
    assert kw["end_conference_on_exit"] is False
    assert env.events[0]["action"] == "add_call" and env.events[0]["participant_call_sid"] == EXTRA
    assert len(env.spawned) == 1


def test_add_call_respects_the_five_party_cap(env):
    parts = {f"CA{i:032x}": SimpleNamespace(call_sid=f"CA{i:032x}", hold=False, status="connected") for i in range(3)}
    parts[PARENT] = SimpleNamespace(call_sid=PARENT, hold=False, status="connected")
    parts[CHILD] = SimpleNamespace(call_sid=CHILD, hold=False, status="connected")
    env.twilio = FakeTwilio(participants=parts)
    r = _client().post("/dialer/call-control/participants", json={
        "parent_call_sid": PARENT, "target": {"type": "number", "phone": "+14155550123"},
    })
    assert r.status_code == 409 and "5" in r.json()["detail"]
    assert not any(op[0] == "create" for op in env.twilio.log)


def test_blocked_number_is_not_dialled(env, monkeypatch):
    monkeypatch.setattr(cc, "_number_block_reason", AsyncMock(return_value="This number is on the Do Not Call list."))
    r = _client().post("/dialer/call-control/participants", json={
        "parent_call_sid": PARENT, "target": {"type": "number", "phone": "+14155550123"},
    })
    assert r.status_code == 409 and "Do Not Call" in r.json()["detail"]
    assert env.twilio.log == []


def test_teammate_invite_goes_through_the_atomic_offer_rpc(env):
    r = _client().post("/dialer/call-control/participants", json={
        "parent_call_sid": PARENT, "target": {"type": "teammate", "user_id": TEAMMATE},
    })
    assert r.status_code == 200
    name, body = env.rpc_calls[0]
    assert name == "dialer_call_control_offer_teammate"
    assert body["p_rep_user_id"] == REP and body["p_target_user_id"] == TEAMMATE
    assert body["p_offer_id"].startswith("xfer-")
    assert env.events[0]["target_label"] == "Daniel"
    assert any(p["label"] == "Daniel" and p["status"] == "connecting" for p in r.json()["participants"])


def test_unavailable_teammate_is_refused(env, monkeypatch):
    monkeypatch.setattr(cc, "_rpc", AsyncMock(return_value=[{"inbound_call_id": None, "target_name": None, "reason": "unavailable"}]))
    r = _client().post("/dialer/call-control/participants", json={
        "parent_call_sid": PARENT, "target": {"type": "teammate", "user_id": TEAMMATE},
    })
    assert r.status_code == 409 and "not available" in r.json()["detail"]
    assert env.events == []


# ── Transfer ─────────────────────────────────────────────────────────────────────

def test_agent_transfer_is_refused_with_reason(env):
    r = _client().post("/dialer/call-control/transfer", json={
        "parent_call_sid": PARENT, "mode": "cold", "target": {"type": "agent"},
    })
    assert r.status_code == 409 and "AI agent" in r.json()["detail"]


def test_cold_transfer_holds_prospect_then_rep_leaves_once_target_connects(env):
    r = _client().post("/dialer/call-control/transfer", json={
        "parent_call_sid": PARENT, "mode": "cold", "target": {"type": "number", "phone": "+14155550123"},
    })
    assert r.status_code == 200
    assert ("update", CHILD, {"hold": True, "hold_url": cc.HOLD_URL, "hold_method": "GET"}) in env.twilio.log
    assert env.events[0]["held_prospect"] is True
    # The target answers; the watcher hands the call over.
    env.twilio.conf.participants[EXTRA].status = "connected"
    env.twilio.log.clear()
    watcher = env.spawned.pop()
    asyncio.run(watcher)
    log = env.twilio.log
    assert log.index(("update", EXTRA, {"end_conference_on_exit": False})) < log.index(("update", EXTRA, {"end_conference_on_exit": True}))
    assert ("update", CHILD, {"hold": False}) in log
    # The rep must stop ending the conference BEFORE leaving it, or everyone is cut off.
    assert log.index(("update", PARENT, {"end_conference_on_exit": False})) < log.index(("delete", PARENT))
    assert env.events[0]["status"] == "completed"


def test_cold_transfer_that_never_connects_is_withdrawn_and_unheld(env, monkeypatch):
    monkeypatch.setattr(cc, "TARGET_CONNECT_SECONDS", 0)
    monkeypatch.setattr(cc, "WATCH_INTERVAL_SECONDS", 0)
    _client().post("/dialer/call-control/transfer", json={
        "parent_call_sid": PARENT, "mode": "cold", "target": {"type": "number", "phone": "+14155550123"},
    })
    env.twilio.log.clear()
    asyncio.run(env.spawned.pop())
    assert ("delete", EXTRA) in env.twilio.log
    assert ("update", CHILD, {"hold": False}) in env.twilio.log
    assert not any(op == ("delete", PARENT) for op in env.twilio.log)
    assert env.events[0]["status"] == "failed"


def test_warm_transfer_complete_and_cancel(env):
    client = _client()
    client.post("/dialer/call-control/transfer", json={
        "parent_call_sid": PARENT, "mode": "warm", "target": {"type": "number", "phone": "+14155550123"},
    })
    # Not joined yet: completing is refused and the rep stays.
    r = client.post("/dialer/call-control/transfer/complete", json={"parent_call_sid": PARENT})
    assert r.status_code == 409
    env.twilio.conf.participants[EXTRA].status = "connected"
    asyncio.run(env.spawned.pop())
    assert env.events[0]["status"] == "connected"
    assert ("delete", PARENT) not in env.twilio.log  # warm: rep is still briefing
    r = client.post("/dialer/call-control/transfer/complete", json={"parent_call_sid": PARENT})
    assert r.status_code == 200
    assert ("delete", PARENT) in env.twilio.log
    assert env.events[0]["status"] == "completed"


def test_warm_transfer_cancel_removes_target_and_unholds(env):
    client = _client()
    client.post("/dialer/call-control/transfer", json={
        "parent_call_sid": PARENT, "mode": "warm", "target": {"type": "number", "phone": "+14155550123"},
    })
    r = client.post("/dialer/call-control/transfer/cancel", json={"parent_call_sid": PARENT})
    assert r.status_code == 200
    assert ("delete", EXTRA) in env.twilio.log
    assert env.twilio.log[-1] == ("update", CHILD, {"hold": False}) or ("update", CHILD, {"hold": False}) in env.twilio.log
    assert env.events[0]["status"] == "cancelled"
    assert ("delete", PARENT) not in env.twilio.log


def test_second_transfer_while_one_is_pending_is_refused(env):
    client = _client()
    client.post("/dialer/call-control/transfer", json={
        "parent_call_sid": PARENT, "mode": "warm", "target": {"type": "number", "phone": "+14155550123"},
    })
    r = client.post("/dialer/call-control/transfer", json={
        "parent_call_sid": PARENT, "mode": "cold", "target": {"type": "teammate", "user_id": TEAMMATE},
    })
    assert r.status_code == 409


def test_remove_participant_refuses_rep_and_prospect(env):
    r = _client().delete(f"/dialer/call-control/participants/{CHILD}", params={"parent_call_sid": PARENT})
    assert r.status_code == 409
    assert env.twilio.log == []
