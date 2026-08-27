"""Unit tests for the SignalWire sales-dialer webhooks.

The payload shape SignalWire actually posts is unverified, so a large slice
of these tests is specifically about the handlers coping with the shape being
different from whatever we guessed - every plausible location for the lead
number, and "never raise, always return SWML" for everything else.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services.telephony.dialer.signalwire_routes import (
    handle_sw_call_status,
    handle_sw_dialer_connect,
    handle_sw_recording,
    normalize_lead_number,
)

_MODULE = "api.services.telephony.dialer.signalwire_routes"

_SECRET = "shhh"


def _request(body, *, query: dict | None = None) -> MagicMock:
    """A Request stand-in carrying a JSON body and query params."""
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    request = MagicMock()
    request.body = AsyncMock(return_value=raw)
    request.query_params = dict(query or {})
    return request


def _authed(body, *, query: dict | None = None) -> MagicMock:
    q = {"k": _SECRET}
    q.update(query or {})
    return _request(body, query=q)


def _payload_of(response) -> dict:
    return json.loads(response.body)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SIGNALWIRE_WEBHOOK_KEY", _SECRET)
    monkeypatch.setenv("SIGNALWIRE_DEFAULT_CALLER_ID", "+12092669253")
    yield


@pytest.fixture
def connect_deps():
    """Patch every outbound dependency of /sw-dialer-connect at once."""
    with patch(f"{_MODULE}.create_dialer_call", new=AsyncMock()) as create, patch(
        f"{_MODULE}.resolve_assigned_caller_id", new=AsyncMock(return_value=None)
    ) as assigned, patch(
        f"{_MODULE}._is_signalwire_owned_number", new=AsyncMock(return_value=False)
    ) as owned, patch(
        f"{_MODULE}._rep_supabase_id",
        new=AsyncMock(return_value="00000000-0000-0000-0000-000000000001"),
    ) as rep, patch(
        f"{_MODULE}.get_backend_endpoints",
        new=AsyncMock(return_value=("https://api.sysevo.io", "wss://api.sysevo.io")),
    ) as endpoints:
        yield {
            "create": create,
            "assigned": assigned,
            "owned": owned,
            "rep": rep,
            "endpoints": endpoints,
        }


def _connect_verb(document: dict) -> dict | None:
    for step in document["sections"]["main"]:
        if "connect" in step:
            return step["connect"]
    return None


def _is_hangup(document: dict) -> bool:
    return document == {"sections": {"main": [{"hangup": {}}]}}


# --------------------------------------------------------------------------
# Lead-number extraction: every plausible location, and normalisation.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body,query",
    [
        ({"params": {"lead": "+14155550123"}}, {}),
        ({"vars": {"userVariables": {"lead": "+14155550123"}}}, {}),
        ({"call": {"params": {"lead": "+14155550123"}}}, {}),
        ({"userVariables": {"lead": "+14155550123"}}, {}),
        ({"lead": "+14155550123"}, {}),
        ({"call": {"lead": "+14155550123"}}, {}),
        ({"call": {"vars": {"userVariables": {"lead": "+14155550123"}}}}, {}),
        ({"vars": {"lead": "+14155550123"}}, {}),
        ({"variables": {"lead": "+14155550123"}}, {}),
        ({"data": {"lead": "+14155550123"}}, {}),
        ({}, {"lead": "+14155550123"}),
        # Alternative key names in the most likely container.
        ({"params": {"lead_number": "+14155550123"}}, {}),
        ({"params": {"to": "+14155550123"}}, {}),
        ({"params": {"To": "+14155550123"}}, {}),
    ],
)
async def test_lead_number_found_in_every_plausible_location(connect_deps, body, query):
    response = await handle_sw_dialer_connect(_authed(body, query=query))
    document = _payload_of(response)
    assert _connect_verb(document)["to"] == "+14155550123"


async def test_payload_location_priority_prefers_params(connect_deps):
    body = {
        "params": {"lead": "+14155550001"},
        "vars": {"userVariables": {"lead": "+14155550002"}},
        "lead": "+14155550003",
    }
    document = _payload_of(await handle_sw_dialer_connect(_authed(body)))
    assert _connect_verb(document)["to"] == "+14155550001"


async def test_query_string_is_the_last_resort_not_the_first(connect_deps):
    request = _authed({"params": {"lead": "+14155550001"}}, query={"lead": "+14155559999"})
    document = _payload_of(await handle_sw_dialer_connect(request))
    assert _connect_verb(document)["to"] == "+14155550001"


async def test_unusable_high_priority_candidate_falls_through_to_a_usable_one(connect_deps):
    """A wrong guess about the payload shape must not be fatal: if the
    highest-priority location holds something undialable, the next candidate
    is tried rather than the call being dropped."""
    body = {
        "params": {"to": "sip:something@example.com"},
        "vars": {"userVariables": {"lead": "+14155550123"}},
    }
    document = _payload_of(await handle_sw_dialer_connect(_authed(body)))
    assert _connect_verb(document)["to"] == "+14155550123"


async def test_signalwire_resource_address_is_never_mistaken_for_a_lead(connect_deps):
    """"destination" in SignalWire's vocabulary is the Fabric resource the
    browser dialled, not the lead - it must not shadow the real number."""
    body = {
        "params": {"destination": "/public/sysevo-dialer?channel=audio"},
        "vars": {"userVariables": {"lead": "+14155550123"}},
    }
    document = _payload_of(await handle_sw_dialer_connect(_authed(body)))
    assert _connect_verb(document)["to"] == "+14155550123"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+14155550123", "+14155550123"),
        ("4155550123", "+14155550123"),
        ("14155550123", "+14155550123"),
        ("(415) 555-0123", "+14155550123"),
        ("415-555-0123", "+14155550123"),
        (" +1 415 555 0123 ", "+14155550123"),
        ("447700900123", "+447700900123"),
        ("+44 7700 900123", "+447700900123"),
    ],
)
def test_normalize_lead_number_accepts(raw, expected):
    assert normalize_lead_number(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        None,
        "abc",
        "415555012a",
        "12345",  # too short for E.164
        "+0155550123",  # E.164 forbids a leading zero country code
        "+1415555012345678901",  # too long
        "+",
    ],
)
def test_normalize_lead_number_rejects(raw):
    assert normalize_lead_number(raw) is None


@pytest.mark.parametrize(
    "body", [{}, {"params": {}}, {"params": {"lead": "not-a-number"}}, {"lead": "12345"}]
)
async def test_missing_or_invalid_lead_returns_hangup(connect_deps, body):
    document = _payload_of(await handle_sw_dialer_connect(_authed(body)))
    assert _is_hangup(document)
    connect_deps["create"].assert_not_called()


# --------------------------------------------------------------------------
# Caller ID: must be a number SignalWire owns.
# --------------------------------------------------------------------------


async def test_caller_id_falls_back_to_env_default_when_rep_unassigned(connect_deps):
    connect_deps["assigned"].return_value = None
    document = _payload_of(
        await handle_sw_dialer_connect(_authed({"params": {"lead": "+14155550123"}}))
    )
    assert _connect_verb(document)["from"] == "+12092669253"


async def test_assigned_twilio_number_is_rejected_in_favour_of_env_default(connect_deps):
    # The realistic case: dialer_phone_numbers has a Twilio number for this
    # rep. SignalWire would reject it outright as a caller ID.
    connect_deps["assigned"].return_value = "+15551230000"
    connect_deps["owned"].return_value = False

    document = _payload_of(
        await handle_sw_dialer_connect(
            _authed({"params": {"lead": "+14155550123", "rep": "rep-42"}})
        )
    )
    assert _connect_verb(document)["from"] == "+12092669253"


async def test_assigned_number_is_used_when_marked_signalwire_owned(connect_deps):
    connect_deps["assigned"].return_value = "+15551230000"
    connect_deps["owned"].return_value = True

    document = _payload_of(
        await handle_sw_dialer_connect(
            _authed({"params": {"lead": "+14155550123", "rep": "rep-42"}})
        )
    )
    assert _connect_verb(document)["from"] == "+15551230000"


async def test_assigned_number_equal_to_env_default_skips_the_ownership_lookup(connect_deps):
    connect_deps["assigned"].return_value = "+12092669253"

    document = _payload_of(
        await handle_sw_dialer_connect(_authed({"params": {"lead": "+14155550123"}}))
    )
    assert _connect_verb(document)["from"] == "+12092669253"
    connect_deps["owned"].assert_not_called()


async def test_no_caller_id_at_all_hangs_up(connect_deps, monkeypatch):
    monkeypatch.delenv("SIGNALWIRE_DEFAULT_CALLER_ID", raising=False)
    connect_deps["assigned"].return_value = None

    document = _payload_of(
        await handle_sw_dialer_connect(_authed({"params": {"lead": "+14155550123"}}))
    )
    assert _is_hangup(document)


async def test_identity_is_read_from_plausible_locations(connect_deps):
    connect_deps["assigned"].return_value = None
    await handle_sw_dialer_connect(
        _authed({"params": {"lead": "+14155550123", "identity": "client:rep-9"}})
    )
    connect_deps["assigned"].assert_awaited_with("client:rep-9")


# --------------------------------------------------------------------------
# Row creation.
# --------------------------------------------------------------------------


async def test_row_is_created_with_provider_signalwire(connect_deps):
    await handle_sw_dialer_connect(
        _authed(
            {
                "params": {
                    "lead": "415-555-0123",
                    "entry": "entry-77",
                    "call_id": "sw-call-1",
                }
            }
        )
    )
    kwargs = connect_deps["create"].call_args.kwargs
    assert kwargs["provider"] == "signalwire"
    assert kwargs["parent_call_sid"] == "sw-call-1"
    assert kwargs["entry_id"] == "entry-77"
    assert kwargs["to_number"] == "+14155550123"
    assert kwargs["from_number"] == "+12092669253"


@pytest.mark.parametrize(
    "body",
    [
        {"params": {"lead": "+14155550123", "entry": "e1"}},
        {"vars": {"userVariables": {"lead": "+14155550123", "entry_id": "e1"}}},
        {"params": {"lead": "+14155550123"}, "entryId": "e1"},
    ],
)
async def test_entry_id_found_in_plausible_locations(connect_deps, body):
    await handle_sw_dialer_connect(_authed(body))
    assert connect_deps["create"].call_args.kwargs["entry_id"] == "e1"


async def test_synthetic_call_id_when_payload_carries_none(connect_deps):
    await handle_sw_dialer_connect(_authed({"params": {"lead": "+14155550123"}}))
    assert connect_deps["create"].call_args.kwargs["parent_call_sid"].startswith("sw-")


async def test_unmappable_rep_still_connects_the_call(connect_deps):
    connect_deps["rep"].return_value = None
    document = _payload_of(
        await handle_sw_dialer_connect(_authed({"params": {"lead": "+14155550123"}}))
    )
    assert _connect_verb(document)["to"] == "+14155550123"
    connect_deps["create"].assert_not_called()


# --------------------------------------------------------------------------
# Recording webhook wiring.
# --------------------------------------------------------------------------


async def test_recording_webhook_points_at_sw_recording_with_secret(connect_deps):
    document = _payload_of(
        await handle_sw_dialer_connect(
            _authed({"params": {"lead": "+14155550123", "call_id": "sw-1"}})
        )
    )
    record = next(s["record_call"] for s in document["sections"]["main"] if "record_call" in s)
    assert record["status_url"] == (
        "https://api.sysevo.io/api/v1/telephony/sw-recording?call_id=sw-1&k=shhh"
    )


async def test_recording_webhook_escapes_a_hostile_call_id(connect_deps):
    """call_id comes from a payload we do not control - an unescaped "&" in
    it would rewrite the query string and drop the secret."""
    document = _payload_of(
        await handle_sw_dialer_connect(
            _authed({"params": {"lead": "+14155550123", "call_id": "a&k=evil b"}})
        )
    )
    record = next(s["record_call"] for s in document["sections"]["main"] if "record_call" in s)
    assert record["status_url"] == (
        "https://api.sysevo.io/api/v1/telephony/sw-recording"
        "?call_id=a%26k%3Devil+b&k=shhh"
    )


async def test_unresolvable_backend_endpoint_omits_recording_but_still_connects(connect_deps):
    connect_deps["endpoints"].side_effect = RuntimeError("no tunnel")
    document = _payload_of(
        await handle_sw_dialer_connect(_authed({"params": {"lead": "+14155550123"}}))
    )
    assert not any("record_call" in s for s in document["sections"]["main"])
    assert _connect_verb(document)["to"] == "+14155550123"


async def test_relative_backend_endpoint_omits_recording(connect_deps):
    connect_deps["endpoints"].return_value = ("", "")
    document = _payload_of(
        await handle_sw_dialer_connect(_authed({"params": {"lead": "+14155550123"}}))
    )
    assert not any("record_call" in s for s in document["sections"]["main"])


# --------------------------------------------------------------------------
# Shared-secret auth.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("query", [{}, {"k": ""}, {"k": "wrong"}, {"k": "shhh "}])
async def test_bad_secret_returns_hangup_and_touches_nothing(connect_deps, query):
    request = _request({"params": {"lead": "+14155550123"}}, query=query)
    document = _payload_of(await handle_sw_dialer_connect(request))
    assert _is_hangup(document)
    connect_deps["create"].assert_not_called()


async def test_unset_secret_allows_the_request(connect_deps, monkeypatch):
    monkeypatch.delenv("SIGNALWIRE_WEBHOOK_KEY", raising=False)
    document = _payload_of(
        await handle_sw_dialer_connect(_request({"params": {"lead": "+14155550123"}}))
    )
    assert _connect_verb(document)["to"] == "+14155550123"


# --------------------------------------------------------------------------
# Nothing escapes as a 500.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"not json at all",
        b"",
        b"[1,2,3]",
        b'"just a string"',
        b"lead=%2B14155550123",  # form-encoded, not JSON
    ],
)
async def test_unparseable_bodies_never_raise(connect_deps, body):
    response = await handle_sw_dialer_connect(_authed(body))
    assert response.status_code == 200
    _payload_of(response)  # must be valid JSON either way


async def test_form_encoded_body_still_finds_the_lead(connect_deps):
    document = _payload_of(await handle_sw_dialer_connect(_authed(b"lead=%2B14155550123")))
    assert _connect_verb(document)["to"] == "+14155550123"


async def test_exploding_dependency_returns_hangup_not_500(connect_deps):
    connect_deps["create"].side_effect = RuntimeError("supabase down")
    document = _payload_of(
        await handle_sw_dialer_connect(_authed({"params": {"lead": "+14155550123"}}))
    )
    assert _is_hangup(document)


async def test_unreadable_body_returns_hangup_not_500(connect_deps):
    request = MagicMock()
    request.body = AsyncMock(side_effect=RuntimeError("connection reset"))
    request.query_params = {"k": _SECRET}
    document = _payload_of(await handle_sw_dialer_connect(request))
    assert _is_hangup(document)


# --------------------------------------------------------------------------
# Status callback.
# --------------------------------------------------------------------------


@pytest.fixture
def status_update():
    with patch(f"{_MODULE}.update_dialer_call_status", new=AsyncMock()) as m:
        yield m


@pytest.mark.parametrize(
    "body,query",
    [
        ({"params": {"call_id": "sw-1", "call_state": "ended", "duration": 42}}, {}),
        ({"call": {"call_id": "sw-1", "state": "ended", "duration": "42"}}, {}),
        ({"call_id": "sw-1", "status": "ended", "call_duration": 42}, {}),
        ({"CallSid": "sw-1", "CallStatus": "ended", "CallDuration": "42"}, {}),
        ({"state": "ended", "duration": 42}, {"call_id": "sw-1"}),
    ],
)
async def test_status_updates_from_plausible_locations(status_update, body, query):
    q = {"k": _SECRET}
    q.update(query)
    response = await handle_sw_call_status(_request(body, query=q))

    assert response.status_code == 200
    kwargs = status_update.call_args.kwargs
    assert kwargs["parent_call_sid"] == "sw-1"
    # Mapped, not passed through. "ended" is not a legal dialer_calls.status -- the CHECK
    # constraint allows completed/busy/failed/no-answer/canceled and friends -- so writing
    # it verbatim would have been rejected and the row left unchanged, silently.
    assert kwargs["status"] == "completed"
    assert kwargs["duration_seconds"] == 42


async def test_status_tolerates_float_duration(status_update):
    await handle_sw_call_status(
        _authed({"call_id": "sw-1", "state": "ended", "duration": "42.7"})
    )
    assert status_update.call_args.kwargs["duration_seconds"] == 42


async def test_status_tolerates_unparseable_duration(status_update):
    await handle_sw_call_status(
        _authed({"call_id": "sw-1", "state": "ended", "duration": "soon"})
    )
    assert status_update.call_args.kwargs["duration_seconds"] is None


@pytest.mark.parametrize("body", [{}, {"call_id": "sw-1"}, {"state": "ended"}])
async def test_status_without_usable_fields_updates_nothing(status_update, body):
    response = await handle_sw_call_status(_authed(body))
    assert response.status_code == 200
    status_update.assert_not_called()


@pytest.mark.parametrize("query", [{}, {"k": "wrong"}])
async def test_status_bad_secret_is_401_not_swml(status_update, query):
    response = await handle_sw_call_status(_request({"call_id": "sw-1"}, query=query))
    assert response.status_code == 401
    status_update.assert_not_called()


async def test_status_never_500s(status_update):
    status_update.side_effect = RuntimeError("supabase down")
    response = await handle_sw_call_status(
        _authed({"call_id": "sw-1", "state": "ended"})
    )
    assert response.status_code == 200


# --------------------------------------------------------------------------
# Recording callback.
# --------------------------------------------------------------------------


@pytest.fixture
def recording_update():
    with patch(f"{_MODULE}.update_dialer_call_recording_url", new=AsyncMock()) as m:
        yield m


@pytest.mark.parametrize(
    "body,query",
    [
        ({"params": {"call_id": "sw-1", "url": "https://rec/a.mp3"}}, {}),
        ({"call_id": "sw-1", "recording_url": "https://rec/a.mp3"}, {}),
        ({"CallSid": "sw-1", "RecordingUrl": "https://rec/a.mp3"}, {}),
        ({"recording": {"url": "https://rec/a.mp3"}}, {"call_id": "sw-1"}),
        ({}, {"call_id": "sw-1", "url": "https://rec/a.mp3"}),
    ],
)
async def test_recording_updates_from_plausible_locations(recording_update, body, query):
    q = {"k": _SECRET}
    q.update(query)
    response = await handle_sw_recording(_request(body, query=q))

    assert response.status_code == 200
    assert recording_update.call_args.kwargs == {
        "parent_call_sid": "sw-1",
        "recording_url": "https://rec/a.mp3",
    }


@pytest.mark.parametrize("body", [{}, {"call_id": "sw-1"}, {"url": "https://rec/a.mp3"}])
async def test_recording_without_usable_fields_updates_nothing(recording_update, body):
    response = await handle_sw_recording(_authed(body))
    assert response.status_code == 200
    recording_update.assert_not_called()


@pytest.mark.parametrize("query", [{}, {"k": "wrong"}])
async def test_recording_bad_secret_is_401_not_swml(recording_update, query):
    response = await handle_sw_recording(
        _request({"call_id": "sw-1", "url": "https://rec/a.mp3"}, query=query)
    )
    assert response.status_code == 401
    recording_update.assert_not_called()


async def test_recording_never_500s(recording_update):
    recording_update.side_effect = RuntimeError("supabase down")
    response = await handle_sw_recording(
        _authed({"call_id": "sw-1", "url": "https://rec/a.mp3"})
    )
    assert response.status_code == 200


# --------------------------------------------------------------------------
# Scope boundary.
# --------------------------------------------------------------------------


def test_signalwire_is_not_in_the_telephony_provider_registry():
    """The hard scope boundary, asserted.

    registry.all_specs() feeds factory.py and the agent pipeline. A
    "signalwire" spec appearing here would make SignalWire selectable for
    AI-agent calls and campaigns - see the comment in api/routes/telephony.py
    on why the dialer router is mounted by hand instead.
    """
    import api.routes.telephony  # noqa: F401 - triggers the router mounting
    from api.services.telephony import registry

    assert "signalwire" not in [spec.name for spec in registry.all_specs()]


def test_signalwire_dialer_routes_are_mounted():
    import api.routes.telephony as telephony_routes

    paths = {route.path for route in telephony_routes.router.routes}
    assert "/telephony/sw-dialer-connect" in paths
    assert "/telephony/sw-call-status" in paths
    assert "/telephony/sw-recording" in paths


# ── Inbound ──────────────────────────────────────────────────────────────────────


async def test_dialer_connect_joins_a_conference_instead_of_dialling():
    """A rep accepting an inbound call has no lead number to dial.

    The conference branch must be checked before the lead lookup: falling through would
    hit the "no dialable lead number" hangup and drop a caller who is already on the line.
    """
    response = await handle_sw_dialer_connect(
        _authed({"vars": {"userVariables": {"rep": "rep-1", "conference": "inbound-abc"}}})
    )
    doc = _payload_of(response)

    assert doc["sections"]["main"] == [{"join_room": {"name": "inbound-abc"}}]


async def test_dialer_connect_still_dials_when_no_conference_is_given():
    """The outbound path must be untouched by the inbound branch."""
    with (
        patch(f"{_MODULE}.create_dialer_call", new=AsyncMock()),
        patch(f"{_MODULE}._rep_supabase_id", new=AsyncMock(return_value=None)),
        patch(f"{_MODULE}.get_backend_endpoints", new=AsyncMock(return_value=("", ""))),
    ):
        response = await handle_sw_dialer_connect(
            _authed({"vars": {"userVariables": {"rep": "rep-1", "lead": "+15550001111"}}})
        )
    doc = _payload_of(response)

    assert any("connect" in step for step in doc["sections"]["main"])


async def test_inbound_holds_the_caller_and_rings_available_reps():
    from api.services.telephony.dialer.signalwire_routes import handle_sw_inbound

    created = {}

    async def _capture(**kwargs):
        created.update(kwargs)
        return {"id": "row-1"}

    with (
        patch(f"{_MODULE}.resolve_inbound_plan", new=AsyncMock(return_value=[
            {"user_id": "user-a", "reach": "live", "forward_number": None},
            {"user_id": "user-b", "reach": "live", "forward_number": None},
        ])),
        patch(f"{_MODULE}.create_inbound_call", new=_capture),
        patch(f"{_MODULE}.get_backend_endpoints", new=AsyncMock(return_value=("", ""))),
    ):
        response = await handle_sw_inbound(
            _authed({"call": {"call_id": "abc", "from": "+447700900000", "to": "+12092669253"}})
        )

    doc = _payload_of(response)
    # The caller is joined to a room, not connected to anyone: nobody has answered yet.
    assert {"join_room": {"name": "inbound-abc"}} in doc["sections"]["main"]
    assert created["target_user_ids"] == ["user-a", "user-b"]
    assert created["conference_name"] == "inbound-abc"


async def test_inbound_records_a_missed_call_when_nobody_is_available():
    """No available reps is a missed call on arrival, not a call left ringing.

    RLS keys visibility on target_user_ids, so a 'ringing' row with no targets would be
    invisible to everyone except a manager and would sit in the list forever.
    """
    from api.services.telephony.dialer.signalwire_routes import handle_sw_inbound

    created = {}

    async def _capture(**kwargs):
        created.update(kwargs)
        return None

    with (
        patch(f"{_MODULE}.resolve_inbound_plan", new=AsyncMock(return_value=[])),
        patch(f"{_MODULE}.create_inbound_call", new=_capture),
        patch(f"{_MODULE}.get_backend_endpoints", new=AsyncMock(return_value=("", ""))),
    ):
        await handle_sw_inbound(
            _authed({"call": {"call_id": "abc", "from": "+447700900000", "to": "+12092669253"}})
        )

    assert created["target_user_ids"] == []


async def test_inbound_tells_the_caller_when_nobody_can_take_the_call():
    """A caller nobody is ringing for must be told, not parked.

    The hold SWML greets with "connecting you now" and joins a conference. With no
    targets, no rep will ever join it, so the caller hears that greeting and then hold
    music until they give up -- while the leg keeps billing.
    """
    from api.services.telephony.dialer.signalwire_routes import handle_sw_inbound

    with (
        patch(f"{_MODULE}.resolve_inbound_plan", new=AsyncMock(return_value=[])),
        patch(f"{_MODULE}.create_inbound_call", new=AsyncMock(return_value=None)),
        patch(f"{_MODULE}.get_backend_endpoints", new=AsyncMock(return_value=("", ""))),
    ):
        response = await handle_sw_inbound(
            _authed({"call": {"call_id": "abc", "from": "+447700900000", "to": "+12092669253"}})
        )

    steps = _payload_of(response)["sections"]["main"]
    assert not any("join_room" in step for step in steps), (
        "no rep is ringing, so joining a conference strands the caller"
    )
    assert steps[-1] == {"hangup": {}}
    assert "say:" in steps[0]["play"]["url"]


async def test_inbound_still_logs_the_call_when_nobody_is_available():
    """Hanging up on the caller must not cost the team the record of them ringing."""
    from api.services.telephony.dialer.signalwire_routes import handle_sw_inbound

    created = {}

    async def _capture(**kwargs):
        created.update(kwargs)
        return None

    with (
        patch(f"{_MODULE}.resolve_inbound_plan", new=AsyncMock(return_value=[])),
        patch(f"{_MODULE}.create_inbound_call", new=_capture),
        patch(f"{_MODULE}.get_backend_endpoints", new=AsyncMock(return_value=("", ""))),
    ):
        await handle_sw_inbound(
            _authed({"call": {"call_id": "abc", "from": "+447700900000", "to": "+12092669253"}})
        )

    assert created["from_number"] == "+447700900000"
    assert created["provider_call_id"] == "abc"


async def test_inbound_normalises_a_sip_caller_id():
    """`from` arrives as a bare number or a SIP URI depending on how the call routed.

    Stored and compared shapes must match, or assignment routing silently never finds the
    number and every call falls back to everyone.
    """
    from api.services.telephony.dialer.signalwire_routes import handle_sw_inbound

    created = {}

    async def _capture(**kwargs):
        created.update(kwargs)
        return None

    with (
        patch(f"{_MODULE}.resolve_inbound_plan", new=AsyncMock(return_value=[{"user_id": "u", "reach": "live", "forward_number": None}])),
        patch(f"{_MODULE}.create_inbound_call", new=_capture),
        patch(f"{_MODULE}.get_backend_endpoints", new=AsyncMock(return_value=("", ""))),
    ):
        await handle_sw_inbound(
            _authed({"call": {
                "call_id": "abc",
                "from": "sip:+447700900000@x.call.signalwire.com;context=private",
                "to": "+12092669253",
            }})
        )

    assert created["from_number"] == "+447700900000"


async def test_inbound_rejects_a_bad_secret():
    from api.services.telephony.dialer.signalwire_routes import handle_sw_inbound

    response = await handle_sw_inbound(
        _request({"call": {"call_id": "abc"}}, query={"k": "wrong"})
    )
    assert _payload_of(response)["sections"]["main"] == [{"hangup": {}}]


async def test_hangup_marks_an_unanswered_call_missed():
    """A caller who gives up while ringing is a missed call, not a call still ringing.

    Patching only ended_at left every unanswered call reading "Ringing" in the history
    forever, with the missed counter stuck at zero -- the number a manager actually acts on.
    """
    from api.services.telephony.dialer.signalwire_routes import handle_sw_inbound_status

    calls = []

    async def _close(*, provider_call_id):
        calls.append(provider_call_id)

    with patch(f"{_MODULE}.close_inbound_call", new=_close):
        result = await handle_sw_inbound_status(
            _authed({"call": {"call_id": "abc"}, "params": {"call_state": "ended"}})
        )

    assert result["status"] == "success"
    assert calls == ["abc"]


async def test_close_inbound_call_only_marks_missed_while_still_ringing():
    """The 'missed' patch must be conditional, or it overwrites a call somebody answered.

    An answered call gets its end time and keeps its status; a race between the rep
    answering and the caller hanging up must not erase the fact that it was picked up.
    """
    from api.services.telephony.dialer.inbound_call_log import close_inbound_call

    seen = []

    async def _update(*, provider_call_id, patch, extra_filters=None):
        seen.append((patch, extra_filters))

    with patch(
        "api.services.telephony.dialer.inbound_call_log.update_inbound_call", new=_update
    ):
        await close_inbound_call(provider_call_id="abc")

    missed_patch, missed_filters = seen[0]
    assert missed_patch["status"] == "missed"
    assert missed_filters == {"status": "eq.ringing"}

    end_patch, end_filters = seen[1]
    assert "status" not in end_patch, "an answered call must keep its status"
    assert end_filters == {"ended_at": "is.null"}


async def test_inbound_refuses_a_payload_with_neither_end_identified():
    """A probe or an unreadable payload must not become a ringing call.

    One did: a row with from='unknown' and to='' rang the whole team, somebody answered
    it, and with no status callback to close it that rep was treated as on a call — and
    so kept out of the ring pool — for an hour.
    """
    from api.services.telephony.dialer.signalwire_routes import handle_sw_inbound

    created = []

    async def _capture(**kwargs):
        created.append(kwargs)
        return None

    with (
        patch(f"{_MODULE}.resolve_inbound_plan", new=AsyncMock(return_value=[{"user_id": "u", "reach": "live", "forward_number": None}])),
        patch(f"{_MODULE}.create_inbound_call", new=_capture),
        patch(f"{_MODULE}.get_backend_endpoints", new=AsyncMock(return_value=("", ""))),
    ):
        response = await handle_sw_inbound(_authed({"call": {"call_id": "probe"}}))

    assert created == [], "no row may be written for a call with neither end identified"
    assert _payload_of(response)["sections"]["main"] == [{"hangup": {}}]


async def test_inbound_still_accepts_a_withheld_caller_id():
    """Withheld number is a real call. Only 'neither end' is refused."""
    from api.services.telephony.dialer.signalwire_routes import handle_sw_inbound

    created = {}

    async def _capture(**kwargs):
        created.update(kwargs)
        return None

    with (
        patch(f"{_MODULE}.resolve_inbound_plan", new=AsyncMock(return_value=[{"user_id": "u", "reach": "live", "forward_number": None}])),
        patch(f"{_MODULE}.create_inbound_call", new=_capture),
        patch(f"{_MODULE}.get_backend_endpoints", new=AsyncMock(return_value=("", ""))),
    ):
        await handle_sw_inbound(
            _authed({"call": {"call_id": "abc", "to": "+13292029868"}})
        )

    assert created["from_number"] == "unknown"
    assert created["to_number"] == "+13292029868"


def test_outbound_connect_omits_ringback():
    """ringback takes play URIs; ["ring"] is not one, and passing it dropped calls."""
    from api.services.telephony.dialer.swml import build_dialer_swml

    doc = build_dialer_swml(
        lead_number="+15550001111", caller_id="+12092669253", recording_webhook=""
    )
    connect = next(s["connect"] for s in doc["sections"]["main"] if "connect" in s)
    assert "ringback" not in connect
    assert connect["timeout"] == 30


def test_outbound_connect_reports_far_end_progress():
    """The only source of truth about whether the callee answered.

    The resource-level Status Change Webhook describes the script's own leg, not the leg
    being dialled, which is why sw-call-status never fired once despite being configured.
    call_state_url is per-connect and reports the B-leg.
    """
    from api.services.telephony.dialer.swml import build_dialer_swml

    doc = build_dialer_swml(
        lead_number="+15550001111",
        caller_id="+12092669253",
        recording_webhook="",
        call_state_webhook="https://api.example.com/sw-call-status?call_id=abc",
    )
    connect = next(s["connect"] for s in doc["sections"]["main"] if "connect" in s)
    assert "answer_on_bridge" not in connect
    assert connect["call_state_url"].endswith("call_id=abc")
    assert "answered" in connect["call_state_events"]
    # No ringback key: it takes play URIs, and the documented default is the provider's
    # own ringback. Passing ["ring"] is what made calls drop after a second.
    assert "ringback" not in connect


async def test_inbound_does_not_wait_for_the_push_before_answering():
    """SWML must come back before a notification is sent, not after.

    A caller is on the line and SignalWire is holding the call waiting for this document.
    Awaiting the push put an HTTP hop to an edge function -- which then calls OneSignal --
    on the critical path: the call rang once and dropped, and the notification landed just
    as it died, because the push was what delayed the answer.
    """
    import asyncio

    from api.services.telephony.dialer import inbound_call_log

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_push(_id: str) -> None:
        started.set()
        await release.wait()

    captured: dict = {}

    async def _fake_post(*args, **kwargs):
        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return [{"id": "row-1"}]

        captured["called"] = True
        return _Resp()

    with (
        patch.object(inbound_call_log, "_notify_targets", new=_slow_push),
        patch.object(inbound_call_log, "SUPABASE_URL", "https://example.supabase.co"),
        patch.object(inbound_call_log, "SUPABASE_SERVICE_ROLE_KEY", "key"),
        patch("httpx.AsyncClient.post", new=_fake_post),
    ):
        # Returns while the push is still in flight. If create_inbound_call awaited it,
        # this would block until release is set and the test would time out.
        row = await asyncio.wait_for(
            inbound_call_log.create_inbound_call(
                provider_call_id="abc",
                from_number="+15550001111",
                to_number="+13292029939",
                conference_name="inbound-abc",
                target_user_ids=["u1"],
            ),
            timeout=2.0,
        )

    assert row == {"id": "row-1"}
    release.set()


async def test_inbound_holds_on_the_sysevo_line_and_never_forwards():
    """A colleague's line behaves like a desk phone: it keeps ringing.

    Forwarding to a personal mobile routed every call to a number that could not be
    reached, turning "nobody is at their screen" into "rings once, then cuts off". The
    caller now waits on the line while a push brings the person to it.
    """
    from api.services.telephony.dialer.signalwire_routes import handle_sw_inbound

    plan = [{"user_id": "u1", "reach": "push", "forward_number": "+447700900123"}]

    with (
        patch(f"{_MODULE}.resolve_inbound_plan", new=AsyncMock(return_value=plan)),
        patch(f"{_MODULE}.create_inbound_call", new=AsyncMock(return_value=None)),
        patch(f"{_MODULE}.get_backend_endpoints", new=AsyncMock(return_value=("", ""))),
    ):
        response = await handle_sw_inbound(
            _authed({"call": {"call_id": "hold", "from": "+15550001111", "to": "+13292029939"}})
        )

    steps = _payload_of(response)["sections"]["main"]
    assert any("join_room" in s for s in steps)
    # A forward_number in the plan must not tempt it: no connect, ever.
    assert not any("connect" in s for s in steps)


async def test_inbound_treats_live_and_push_the_same():
    """Both are somebody we can reach; the difference is only how fast they notice."""
    from api.services.telephony.dialer.signalwire_routes import handle_sw_inbound

    plan = [{"user_id": "u1", "reach": "live", "forward_number": None}]

    with (
        patch(f"{_MODULE}.resolve_inbound_plan", new=AsyncMock(return_value=plan)),
        patch(f"{_MODULE}.create_inbound_call", new=AsyncMock(return_value=None)),
        patch(f"{_MODULE}.get_backend_endpoints", new=AsyncMock(return_value=("", ""))),
    ):
        response = await handle_sw_inbound(
            _authed({"call": {"call_id": "hold2", "from": "+15550001111", "to": "+13292029939"}})
        )

    assert any("join_room" in s for s in _payload_of(response)["sections"]["main"])


async def test_background_push_task_is_held_until_it_finishes():
    """asyncio keeps only a weak reference to a running task.

    A task nobody holds can be collected before it completes, and the failure is silent:
    the push never arrives, with no error and nothing logged. That is indistinguishable
    from the call having died first — the precise confusion this code already caused once.
    """
    import asyncio

    from api.services.telephony.dialer import inbound_call_log

    ran = asyncio.Event()

    async def _work() -> None:
        await asyncio.sleep(0)
        ran.set()

    inbound_call_log._spawn(_work())
    # Held while in flight, so a collection pass cannot take it.
    assert inbound_call_log._background, "the task must be referenced while running"

    await asyncio.wait_for(ran.wait(), timeout=1.0)
    await asyncio.sleep(0)
    # And released once done, so the set is not a leak.
    assert not inbound_call_log._background, "finished tasks must be discarded"
