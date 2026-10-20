"""Call sorter wired into the live dialer routes (F5), behind SYSEVO_DIALER_CALL_SORTER.

Flag OFF: no stream is started, no tap is added, the sorter is never consulted, and
playback TwiML is unchanged. Flag ON:
* Twilio forks the prospect leg from its `in-progress` status callback (single and
  parallel), stops the fork before a recording plays, and serves /dialer-call-sorter;
* SignalWire taps the screened leg before detect_machine and serves /sw-call-sorter;
* an unsure AMD waits for the sorter (4 s); a machine gets a 2 s veto window:
  mailbox_full -> nothing plays, outcome mailbox_full; screener/person -> never a
  voicemail (handled as a person).

Every provider, Redis and database boundary is mocked; no call is placed.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.services.telephony.dialer import sorter_gate
from api.services.telephony.providers.twilio import routes as twilio_routes
from api.services.telephony.providers.twilio.routes import router

_PATH = "api.services.telephony.providers.twilio.routes"
_SW = "api.services.telephony.dialer.signalwire_routes"
_GATE = "api.services.telephony.dialer.sorter_gate"
_PARENT = "CA" + "abcdef0123456789" * 2
_CHILD = "CA" + "0123456789abcdef" * 2
_REP = "11111111-1111-1111-1111-111111111111"
_LEG = "11111111-1111-4111-8111-111111111111"
_ENTRY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_SESSION = "00000000-0000-0000-0000-000000000021"
_SW_LEG = "00000000-0000-0000-0000-000000000022"
_URL = "https://db.example.com/storage/v1/object/sign/voicemail-drops/u/m.wav?token=x"


@pytest.fixture(autouse=True)
def _signed(monkeypatch):
    monkeypatch.setenv("SYSEVO_TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setattr(f"{_PATH}.close_floor_monitor_for_provider", AsyncMock(return_value=False))
    with patch(f"{_PATH}.RequestValidator.validate", return_value=True), \
         patch(f"{_PATH}._resolve_dialer_auth_token", new=AsyncMock(return_value="test-auth-token")):
        yield


@pytest.fixture
def sorter_on(monkeypatch):
    monkeypatch.setenv("SYSEVO_DIALER_CALL_SORTER", "1")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")


@pytest.fixture
def sorter_off(monkeypatch):
    monkeypatch.delenv("SYSEVO_DIALER_CALL_SORTER", raising=False)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ── the gate ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gate_is_inert_with_the_flag_off(sorter_off):
    with patch(f"{_GATE}.call_sorter.wait_for_sorter_stage", new=AsyncMock()) as wait:
        assert await sorter_gate.consult_sorter(_PARENT, amd="machine") == sorter_gate.AS_IS
    wait.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("amd", "stage", "kind", "full", "timeout"), [
    ("machine", "mailbox_full", "machine", True, 2.0),
    ("machine", "screener", "person", False, 2.0),
    ("machine", "person", "person", False, 2.0),
    ("machine", "machine", "machine", False, 2.0),   # incl. screener_unavailable
    ("machine", None, "as_is", False, 2.0),
    ("unsure", "machine", "machine", False, 4.0),
    ("unsure", "beep", "machine", False, 4.0),
    ("unsure", "ivr", "as_is", False, 4.0),
])
async def test_gate_rules(sorter_on, amd, stage, kind, full, timeout):
    state = {"stage": stage} if stage else None
    with patch(f"{_GATE}.call_sorter.wait_for_sorter_stage", new=AsyncMock(return_value=state)) as wait:
        decision = await sorter_gate.consult_sorter(_PARENT, amd=amd)
    assert decision.kind == kind and decision.mailbox_full is full
    assert wait.await_args.kwargs["timeout"] == timeout


@pytest.mark.asyncio
async def test_gate_never_raises(sorter_on):
    with patch(f"{_GATE}.call_sorter.wait_for_sorter_stage", new=AsyncMock(side_effect=RuntimeError("redis"))):
        assert await sorter_gate.consult_sorter(_PARENT, amd="unsure") == sorter_gate.AS_IS


# ── Twilio: starting the fork ───────────────────────────────────────────────

def _status(params: dict, call_status: str = "in-progress"):
    with patch(f"{_PATH}.update_dialer_call_status", new=AsyncMock()), \
         patch(f"{_PATH}._record_single_call_status_stage", new=AsyncMock()), \
         patch(f"{_PATH}.dialer_parallel.record_parallel_timing", new=AsyncMock()), \
         patch(f"{_PATH}.dialer_parallel.update_parallel_leg", new=AsyncMock(return_value=True)), \
         patch(f"{_PATH}.dialer_parallel.parallel_legs", new=AsyncMock(return_value=[{"status": "answered"}])), \
         patch(f"{_PATH}.dialer_parallel.parallel_session", new=AsyncMock(return_value=None)), \
         patch(f"{_PATH}._twilio_client", new=AsyncMock(return_value="client")), \
         patch(f"{_PATH}.get_backend_endpoints", new=AsyncMock(return_value=("https://api.example.com", None))), \
         patch(f"{_PATH}.dialer_call_sorter.start_prospect_stream", new=AsyncMock(return_value="MZ1")) as start:
        response = _app().post(
            "/dialer-call-status", params=params,
            data={"CallSid": _CHILD, "CallStatus": call_status}, headers={"X-Twilio-Signature": "sig"},
        )
    assert response.status_code == 200
    return start


def test_twilio_single_answer_forks_the_prospect_leg(sorter_on):
    start = _status({"parent_call_sid": _PARENT})
    start.assert_awaited_once_with(
        "client", backend_endpoint="https://api.example.com", call_ref=_PARENT,
        leg_call_sid=_CHILD, secret="test-auth-token",
    )


def test_twilio_parallel_answer_forks_the_line_under_its_own_ref(sorter_on):
    start = _status({"parent_call_sid": _PARENT, "parallel_leg_id": _LEG})
    assert start.await_args.kwargs["call_ref"] == _CHILD
    assert start.await_args.kwargs["leg_call_sid"] == _CHILD


def test_twilio_ringing_does_not_fork(sorter_on):
    _status({"parent_call_sid": _PARENT}, call_status="ringing").assert_not_awaited()


def test_twilio_never_forks_with_the_flag_off(sorter_off):
    _status({"parent_call_sid": _PARENT}).assert_not_awaited()
    _status({"parent_call_sid": _PARENT, "parallel_leg_id": _LEG}).assert_not_awaited()


@pytest.mark.asyncio
async def test_twilio_sorter_socket_authenticates_with_the_dialer_auth_token():
    async def serve(websocket, resolve_secret):
        assert await resolve_secret() == "test-auth-token"
    with patch(f"{_PATH}.dialer_call_sorter.serve_twilio_sorter", new=AsyncMock(side_effect=serve)) as served:
        await twilio_routes.handle_dialer_call_sorter(SimpleNamespace())
    served.assert_awaited_once()
    assert "/dialer-call-sorter" in {getattr(r, "path", "") for r in router.routes}


def test_playback_twiml_stops_the_fork_only_with_the_flag_on(monkeypatch):
    kwargs = dict(endpoint_root="https://api.example.com/api/v1/telephony", parent_call_sid=_PARENT,
                  playback_url=_URL, leg_call_sid=_CHILD)
    monkeypatch.delenv("SYSEVO_DIALER_CALL_SORTER", raising=False)
    assert "<Stop>" not in twilio_routes._voicemail_playback_twiml(**kwargs)
    monkeypatch.setenv("SYSEVO_DIALER_CALL_SORTER", "1")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    twiml = twilio_routes._voicemail_playback_twiml(**kwargs)
    assert f'<Response><Stop><Stream name="sorter-{_CHILD}"/></Stop><Play>' in twiml


# ── Twilio: F5 in the AMD answer ────────────────────────────────────────────

def _join(answered_by: str, *, decision: sorter_gate.SorterDecision, enabled: bool = True,
          row: dict | None = None):
    row = row or {"rep_user_id": _REP, "child_call_sid": _CHILD}
    patches = {
        "child": patch(f"{_PATH}.update_dialer_call_child_sid", new=AsyncMock()),
        "amd": patch(f"{_PATH}.update_dialer_call_amd_result", new=AsyncMock(return_value=True)),
        "decision": patch(f"{_PATH}.record_twilio_amd_decision", new=AsyncMock()),
        "row": patch(f"{_PATH}.get_dialer_call_child_leg", new=AsyncMock(return_value=row)),
        "policy": patch(f"{_PATH}.get_team_voicemail_policy", new=AsyncMock(return_value={"on_machine": "drop_vm"})),
        "settings": patch(f"{_PATH}.get_auto_voicemail_settings", new=AsyncMock(
            return_value=(enabled, _URL if enabled else None, "d1" if enabled else None))),
        "claim": patch(f"{_PATH}.claim_manual_voicemail_playback", new=AsyncMock(return_value=True)),
        "attach": patch(f"{_PATH}.attach_voicemail_drop_to_call", new=AsyncMock(return_value=True)),
        "evidence": patch(f"{_PATH}.get_voicemail_drop_evidence", new=AsyncMock(return_value={})),
        "cancel": patch(f"{_PATH}.cancel_call", new=AsyncMock(return_value=True)),
        "failed": patch(f"{_PATH}.record_voicemail_playback_failure", new=AsyncMock(return_value=True)),
        "stage": patch(f"{_PATH}.call_stages.record_call_stage", new=AsyncMock(return_value=True)),
        "consult": patch(f"{_PATH}.sorter_gate.consult_sorter", new=AsyncMock(return_value=decision)),
    }
    import contextlib
    with contextlib.ExitStack() as stack:
        m = {k: stack.enter_context(v) for k, v in patches.items()}
        response = _app().post(
            "/dialer-conference-join",
            params={"conference_name": f"call-{_PARENT}", "parent_call_sid": _PARENT},
            data={"AnsweredBy": answered_by, "CallSid": _CHILD}, headers={"X-Twilio-Signature": "sig"},
        )
    return response, m


def test_twilio_flag_off_never_consults_the_sorter(sorter_off):
    response, m = _join("machine_end_beep", decision=sorter_gate.SorterDecision(kind="person"))
    m["consult"].assert_not_awaited()
    assert "<Play>" in response.text and "<Stop>" not in response.text


def test_twilio_sorter_mailbox_full_vetoes_the_playback(sorter_on):
    response, m = _join("machine_end_beep",
                        decision=sorter_gate.SorterDecision(kind="machine", mailbox_full=True, stage="mailbox_full"))
    assert "<Hangup/>" in response.text and "<Play>" not in response.text
    m["claim"].assert_not_awaited()
    m["failed"].assert_awaited_once_with(parent_call_sid=_PARENT, reason="mailbox_full",
                                         child_call_sid=None, leg_id=None)
    m["cancel"].assert_awaited_once_with(call_sid=_PARENT)   # the rep's leg is released
    assert m["consult"].await_args.kwargs["amd"] == "machine"


def test_twilio_sorter_screener_is_never_a_voicemail(sorter_on):
    response, m = _join("machine_end_beep",
                        decision=sorter_gate.SorterDecision(kind="person", stage="screener"))
    assert "<Play>" not in response.text and "<Conference" in response.text   # the rep stays on
    m["claim"].assert_not_awaited()
    assert m["amd"].await_args.kwargs["answered_by"] == "human"


def test_twilio_unsure_amd_that_the_sorter_calls_a_machine_plays(sorter_on):
    response, m = _join("unknown", decision=sorter_gate.SorterDecision(kind="machine", stage="machine"))
    assert m["consult"].await_args.kwargs["amd"] == "unsure"
    assert "<Play>" in response.text
    assert f'<Stop><Stream name="sorter-{_CHILD}"/></Stop>' in response.text


def test_twilio_sorter_quiet_keeps_the_carrier_verdict(sorter_on):
    response, _m = _join("machine_end_silence", decision=sorter_gate.AS_IS)
    assert "<Play>" in response.text


# ── SignalWire ───────────────────────────────────────────────────────────────

@pytest.fixture
def _sw_env(monkeypatch):
    from api.tests.test_signalwire_dialer_routes import _SECRET
    monkeypatch.setenv("SIGNALWIRE_WEBHOOK_KEY", _SECRET)


def _sw_context(line_count: int = 1, status: str = "ringing"):
    return {
        "session": {"id": _SESSION, "status": "dialing", "rep_user_id": _REP, "rep_call_id": "browser-call",
                    "line_count": line_count, "conference_name": f"sysevo-par-{_SESSION}", "winner_leg_id": None},
        "leg": {"id": _SW_LEG, "provider_call_id": "prospect-call", "entry_id": _ENTRY, "status": status},
    }


async def _sw_leg(line_count: int):
    from api.services.telephony.dialer.signalwire_routes import handle_sw_parallel_leg
    from api.tests.test_signalwire_dialer_routes import _authed
    with patch(f"{_SW}.signalwire_parallel_store.get_leg_context", new=AsyncMock(return_value=_sw_context(line_count))), \
         patch(f"{_SW}.signalwire_parallel_store.update_leg", new=AsyncMock(return_value=True)), \
         patch(f"{_SW}.signalwire_parallel_store.get_voicemail_policy", new=AsyncMock(
             return_value={"machine_detection_enabled": True, "machine_detection_preset": "balanced"})), \
         patch(f"{_SW}.get_backend_endpoints", new=AsyncMock(return_value=("https://api.sysevo.io", None))):
        response = await handle_sw_parallel_leg(_authed(
            {"call": {"call_id": "prospect-call"}}, query={"session_id": _SESSION, "leg_id": _SW_LEG},
        ))
    return json.loads(response.body)["sections"]["main"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("line_count", "call_ref"), [(1, "browser-call"), (3, "prospect-call")])
async def test_signalwire_leg_taps_the_prospect_before_amd(sorter_on, _sw_env, line_count, call_ref):
    steps = await _sw_leg(line_count)
    assert list(steps[0]) == ["tap"] and "detect_machine" in steps[1]
    tap = steps[0]["tap"]
    assert tap["direction"] == "speak" and "/api/v1/telephony/sw-call-sorter?" in tap["uri"]
    assert f"call_ref={call_ref}" in tap["uri"] and "leg=prospect-call" in tap["uri"]


@pytest.mark.asyncio
async def test_signalwire_leg_has_no_tap_with_the_flag_off(sorter_off, _sw_env):
    steps = await _sw_leg(1)
    assert "detect_machine" in steps[0] and not any("tap" in step for step in steps)


@pytest.mark.asyncio
async def test_signalwire_sorter_socket_route_serves_the_sorter():
    from api.services.telephony.dialer import signalwire_routes
    with patch(f"{_SW}.signalwire_call_sorter.serve_signalwire_sorter", new=AsyncMock()) as served:
        await signalwire_routes.websocket_sw_call_sorter("ws")
    served.assert_awaited_once_with("ws")
    assert "/sw-call-sorter" in {getattr(r, "path", "") for r in signalwire_routes.router.routes}


async def _sw_decide(amd_result: str, decision: sorter_gate.SorterDecision, *, policy: dict | None = None):
    from api.services.telephony.dialer.signalwire_routes import handle_sw_parallel_decision
    from api.tests.test_signalwire_dialer_routes import _authed
    request = _authed({"call": {"call_id": "prospect-call", "amd_result": amd_result, "amd_ms": "900", "beep": "true"}},
                      query={"session_id": _SESSION, "leg_id": _SW_LEG})
    spawned = []
    policy = policy or {"on_machine": "drop_vm", "single_auto_send": True, "parallel_auto_send": True}
    patches = {
        "context": patch(f"{_SW}.signalwire_parallel_store.get_leg_context",
                         new=AsyncMock(return_value=_sw_context(status="screening"))),
        "requested": patch(f"{_SW}.signalwire_parallel_store.leg_voicemail_requested", new=AsyncMock(return_value=False)),
        "update_leg": patch(f"{_SW}.signalwire_parallel_store.update_leg", new=AsyncMock(return_value=True)),
        "policy": patch(f"{_SW}.signalwire_parallel_store.get_voicemail_policy", new=AsyncMock(return_value=policy)),
        "drop": patch(f"{_SW}._load_signalwire_parallel_default_drop", new=AsyncMock(
            return_value={"id": "d1", "storage_path": f"{_REP}/m.wav"})),
        "sign": patch(f"{_SW}._signed_signalwire_voicemail_url", new=AsyncMock(return_value="https://db.example.com/m.wav")),
        "claim": patch(f"{_SW}.signalwire_parallel_store.claim_voicemail_drop", new=AsyncMock(return_value=True)),
        "winner": patch(f"{_SW}.signalwire_parallel_store.claim_winner", new=AsyncMock(return_value=True)),
        "outcome": patch(f"{_SW}.signalwire_parallel_store.record_outcome", new=AsyncMock(return_value=True)),
        "amd": patch(f"{_SW}.update_signalwire_dialer_amd", new=AsyncMock(return_value=True)),
        "history": patch(f"{_SW}.update_dialer_call_status", new=AsyncMock()),
        "stage": patch(f"{_SW}.call_stages.record_call_stage", new=AsyncMock(return_value=True)),
        "endpoints": patch(f"{_SW}.get_backend_endpoints", new=AsyncMock(return_value=("https://api.example.test", None))),
        "join": patch(f"{_SW}._signalwire_parallel_join_swml",
                      new=AsyncMock(return_value={"sections": {"main": [{"join_conference": {}}]}})),
        "consult": patch(f"{_SW}.sorter_gate.consult_sorter", new=AsyncMock(return_value=decision)),
        "spawn": patch(f"{_SW}._spawn_background", new=lambda coro: spawned.append(coro)),
    }
    import contextlib
    with contextlib.ExitStack() as stack:
        m = {k: stack.enter_context(v) for k, v in patches.items()}
        response = await handle_sw_parallel_decision(request)
    for coro in spawned:
        coro.close()
    return json.loads(response.body), m


@pytest.mark.asyncio
async def test_signalwire_flag_off_never_consults_the_sorter(sorter_off, _sw_env):
    body, m = await _sw_decide("machine", sorter_gate.SorterDecision(kind="person"))
    m["consult"].assert_not_awaited()
    assert "play" in body["sections"]["main"][0]


@pytest.mark.asyncio
async def test_signalwire_sorter_mailbox_full_vetoes_the_playback(sorter_on, _sw_env):
    body, m = await _sw_decide("machine", sorter_gate.SorterDecision(kind="machine", mailbox_full=True))
    assert body == {"sections": {"main": [{"hangup": {}}]}}
    m["claim"].assert_not_awaited()
    assert m["outcome"].await_args.kwargs["outcome"] == "mailbox_full"
    assert m["consult"].await_args.args == ("browser-call",)


@pytest.mark.asyncio
async def test_signalwire_sorter_screener_goes_to_the_rep_like_a_person(sorter_on, _sw_env):
    body, m = await _sw_decide("machine", sorter_gate.SorterDecision(kind="person", stage="screener"))
    assert "join_conference" in body["sections"]["main"][0]
    m["claim"].assert_not_awaited()
    assert m["winner"].await_args.kwargs["amd_result"] == "human"


@pytest.mark.asyncio
async def test_signalwire_unsure_amd_that_the_sorter_calls_a_machine_plays(sorter_on, _sw_env):
    body, m = await _sw_decide("unknown", sorter_gate.SorterDecision(kind="machine", stage="machine"))
    assert m["consult"].await_args.kwargs["amd"] == "unsure"
    assert "play" in body["sections"]["main"][0]
    m["claim"].assert_awaited_once()
