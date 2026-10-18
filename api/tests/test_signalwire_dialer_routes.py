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
    handle_sw_parallel_conference,
    handle_sw_connect_status,
    handle_sw_call_status,
    handle_sw_dialer_connect,
    handle_sw_recording,
    _redacted_query,
    _map_call_state,
    _secret_ok,
    _webhook_signature,
    _webhook_url,
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
def _dialer_compliance_env(monkeypatch):
    monkeypatch.setenv("SIGNALWIRE_WEBHOOK_KEY", _SECRET)
    monkeypatch.setenv("SIGNALWIRE_DEFAULT_CALLER_ID", "+12092669253")
    monkeypatch.setattr(_MODULE + ".phone_block_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(_MODULE + ".calling_hours_result", AsyncMock(return_value=(True, None, "CA")))
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


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("noAnswer", "no-answer"),
        ("no_answer", "no-answer"),
        ("NO-ANSWER", "no-answer"),
        ("busy", "busy"),
        ("declined", "canceled"),
        ("decline", "canceled"),
        ("abandoned", "canceled"),
        ("not_found", "failed"),
        ("max_duration", "completed"),
        ("cancel", "canceled"),
        ("error", "failed"),
    ],
)
def test_signalwire_terminal_reason_aliases(reason, expected):
    assert _map_call_state("ended", reason) == expected


def test_dynamic_webhook_uses_call_scoped_signature_without_shared_key(monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    monkeypatch.delenv("SIGNALWIRE_WEBHOOK_KEY", raising=False)
    monkeypatch.setenv("SIGNALWIRE_API_TOKEN", "provider-secret")
    url = _webhook_url("https://api.example.com", "sw-call-status", "call-123")
    query = parse_qs(urlsplit(url).query)
    assert query["call_id"] == ["call-123"]
    assert query["sig"] == [
        _webhook_signature("call-123", int(query["exp"][0]))
    ]
    assert "k" not in query

    request = _request({}, query={key: values[0] for key, values in query.items()})
    assert _secret_ok("sw-call-status", request, allow_per_call_signature=True)
    wrong_call = _request({}, query={**request.query_params, "call_id": "call-456"})
    assert not _secret_ok(
        "sw-call-status", wrong_call, allow_per_call_signature=True
    )


def test_dynamic_webhook_fails_closed_without_a_valid_signature(monkeypatch):
    monkeypatch.delenv("SIGNALWIRE_WEBHOOK_KEY", raising=False)
    monkeypatch.setenv("SIGNALWIRE_API_TOKEN", "provider-secret")
    assert not _secret_ok(
        "sw-recording", _request({}, query={"call_id": "call-123"}),
        allow_per_call_signature=True,
    )
    assert not _secret_ok(
        "sw-recording",
        _request({}, query={"call_id": "call-123", "exp": "1999999999", "sig": "wrong"}),
        allow_per_call_signature=True,
    )


def test_pre_redaction_callback_signature_is_no_longer_valid(monkeypatch):
    import hashlib
    import hmac
    import time

    monkeypatch.delenv("SIGNALWIRE_WEBHOOK_KEY", raising=False)
    monkeypatch.setenv("SIGNALWIRE_API_TOKEN", "provider-secret")
    expires = int(time.time()) + 3600
    old_sig = hmac.new(
        b"provider-secret",
        f"webhook:call-123:{expires}".encode(),
        hashlib.sha256,
    ).hexdigest()
    request = _request(
        {}, query={"call_id": "call-123", "exp": str(expires), "sig": old_sig}
    )
    assert not _secret_ok(
        "sw-call-status", request, allow_per_call_signature=True
    )


async def test_signalwire_parallel_conference_records_rep_join():
    session_id = "00000000-0000-0000-0000-000000000010"
    body = {
        "event_type": "calling.conference",
        "timestamp": 1_800_000_000.125,
        "params": {
            "status": "participant-join",
            "call_id": "browser-call-0001",
            "name": "sysevo-par-00000000-0000-0000-0000-000000000011",
        },
    }
    with patch(
        f"{_MODULE}.signalwire_parallel_store.get_session",
        new=AsyncMock(return_value={
            "rep_call_id": "browser-call-0001",
            "conference_name": body["params"]["name"],
        }),
    ), patch(
        f"{_MODULE}.signalwire_parallel_store.record_parallel_timing",
        new=AsyncMock(return_value=True),
    ) as record:
        response = await handle_sw_parallel_conference(_authed(
            body, query={"session_id": session_id, "leg_id": "rep"}
        ))
    assert response.status_code == 200
    assert _payload_of(response) == {"ok": True, "ignored": False}
    assert record.await_args.kwargs["event_kind"] == "rep_join"
    assert record.await_args.kwargs["provider_call_id"] == "browser-call-0001"


@pytest.mark.parametrize("event,dependency", [
    ("participant-join", "record_overflow_join"),
    ("participant-leave", "close_overflow_conference"),
])
async def test_overflow_conference_uses_bound_participant(event, dependency):
    from api.services.telephony.dialer.signalwire_routes import handle_sw_overflow_conference

    conference = "sysevo-overflow-00000000-0000-0000-0000-000000000010"
    body = {"timestamp": 1_800_000_000.125, "params": {
        "name": conference, "status": event, "call_id": "bound-browser-call",
    }}
    with patch(f"{_MODULE}.{dependency}", new=AsyncMock(return_value=True)) as record:
        response = await handle_sw_overflow_conference(_authed(body, query={"call_id": conference}))
    assert response.status_code == 200
    assert record.await_args.kwargs["conference_name"] == conference
    assert record.await_args.kwargs["participant_call_id"] == "bound-browser-call"
    assert record.await_args.kwargs["provider"] == "signalwire"
    if event == "participant-join":
        assert record.await_args.kwargs["event_at"] == "2027-01-15T08:00:00.125000+00:00"


async def test_overflow_conference_rejects_callback_for_another_room():
    from api.services.telephony.dialer.signalwire_routes import handle_sw_overflow_conference

    conference = "sysevo-overflow-00000000-0000-0000-0000-000000000010"
    with patch(f"{_MODULE}.record_overflow_join", new=AsyncMock()) as record:
        response = await handle_sw_overflow_conference(_authed({"params": {
            "name": conference, "status": "participant-join", "call_id": "forged-participant",
        }}, query={"call_id": "another-room"}))
    assert response.status_code == 409
    record.assert_not_awaited()


async def test_overflow_conference_database_failure_is_retryable():
    from api.services.telephony.dialer.signalwire_routes import handle_sw_overflow_conference

    conference = "sysevo-overflow-00000000-0000-0000-0000-000000000010"
    with patch(f"{_MODULE}.record_overflow_join", new=AsyncMock(return_value=None)):
        response = await handle_sw_overflow_conference(_authed({
            "timestamp": 1_800_000_000.125, "params": {
                "name": conference, "status": "participant-join", "call_id": "bound-participant",
            },
        }, query={"call_id": conference}))
    assert response.status_code == 503


async def test_signalwire_parallel_conference_records_winner_join():
    session_id = "00000000-0000-0000-0000-000000000010"
    leg_id = "00000000-0000-0000-0000-000000000012"
    body = {
        "event_type": "calling.conference",
        "timestamp": 1_800_000_000.125,
        "params": {
            "status": "participant-join",
            "call_id": "pstn-call-0001",
            "name": "sysevo-par-00000000-0000-0000-0000-000000000011",
        },
    }
    with patch(
        f"{_MODULE}.signalwire_parallel_store.get_session",
        new=AsyncMock(return_value={
            "conference_name": body["params"]["name"],
            "winner_leg_id": leg_id,
        }),
    ), patch(
        f"{_MODULE}.signalwire_parallel_store.get_session_legs",
        new=AsyncMock(return_value=[{"id": leg_id, "provider_call_id": "pstn-call-0001"}]),
    ), patch(
        f"{_MODULE}.signalwire_parallel_store.record_parallel_timing",
        new=AsyncMock(return_value=True),
    ) as record:
        response = await handle_sw_parallel_conference(_authed(
            body, query={"session_id": session_id, "leg_id": leg_id}
        ))
    assert response.status_code == 200
    assert record.await_args.kwargs["event_kind"] == "lead_join"
    assert record.await_args.kwargs["leg_id"] == leg_id


def test_signed_callback_secret_is_redacted_from_logs():
    request = _request(
        {}, query={"call_id": "call-123", "exp": "12345", "sig": "secret-digest"}
    )
    assert _redacted_query(request) == {
        "call_id": "call-123",
        "exp": "12345",
        "sig": "<redacted>",
    }


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


async def test_assigned_number_not_confirmed_as_signalwire_is_rejected(connect_deps):
    # A stale SignalWire client must not silently substitute another line.
    connect_deps["assigned"].return_value = "+15551230000"
    connect_deps["owned"].return_value = False

    with patch(f"{_MODULE}.update_dialer_call_status", new=AsyncMock()) as update:
        document = _payload_of(
            await handle_sw_dialer_connect(
                _authed({"params": {"lead": "+14155550123", "rep": "rep-42"}})
            )
        )
    assert _is_hangup(document)
    connect_deps["create"].assert_awaited_once()
    assert connect_deps["create"].await_args.kwargs["from_number"] == ""
    assert connect_deps["create"].await_args.kwargs["provider"] == "signalwire"
    update.assert_awaited_once()
    assert update.await_args.kwargs["status"] == "failed"


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
    connect_deps["assigned"].assert_awaited_with("client:rep-9", provider_name="signalwire")


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


async def test_signalwire_outbound_blocks_suppressed_number_before_call_record(connect_deps, monkeypatch):
    monkeypatch.setattr(_MODULE + ".phone_block_reason", AsyncMock(return_value="do_not_call"))
    response = await handle_sw_dialer_connect(
        _authed({"params": {"lead": "+15559876543", "identity": "client:rep-9", "call_id": "blocked"}})
    )

    assert _payload_of(response) == {"sections": {"main": [{"hangup": {}}]}}
    connect_deps["create"].assert_not_awaited()


async def test_signalwire_outbound_blocks_outside_calling_hours_before_call_record(connect_deps, monkeypatch):
    monkeypatch.setattr(
        _MODULE + ".calling_hours_result",
        AsyncMock(return_value=(False, "outside_calling_hours", "CA")),
    )
    response = await handle_sw_dialer_connect(
        _authed({"params": {"lead": "+15559876543", "identity": "client:rep-9", "call_id": "blocked-hours"}})
    )

    assert _is_hangup(_payload_of(response))
    connect_deps["create"].assert_not_awaited()


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


async def test_unmappable_rep_fails_closed_before_call_record_or_origination(connect_deps):
    connect_deps["rep"].return_value = None
    document = _payload_of(
        await handle_sw_dialer_connect(
            _authed({"params": {"lead": "+14155550123", "identity": "client:rep-9"}})
        )
    )
    assert _is_hangup(document)
    connect_deps["create"].assert_not_awaited()


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


async def test_unset_secret_rejects_outbound_call_instructions(connect_deps, monkeypatch):
    monkeypatch.delenv("SIGNALWIRE_WEBHOOK_KEY", raising=False)
    monkeypatch.delenv("SIGNALWIRE_DIALER_CONNECT_KEY", raising=False)
    document = _payload_of(
        await handle_sw_dialer_connect(_request({"params": {"lead": "+14155550123"}}))
    )
    assert _is_hangup(document)
    connect_deps["create"].assert_not_called()


def test_static_inbound_swml_requires_configured_key(monkeypatch):
    monkeypatch.delenv("SIGNALWIRE_WEBHOOK_KEY", raising=False)
    monkeypatch.delenv("SIGNALWIRE_DIALER_CONNECT_KEY", raising=False)
    assert not _secret_ok("sw-inbound", _request({}))


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


@pytest.mark.parametrize(
    "end_source,expected",
    [("none", "failed"), ("outbound", "completed"), ("inbound", "completed")],
)
async def test_status_does_not_misclassify_unconnected_leg_as_completed(
    status_update, end_source, expected
):
    await handle_sw_call_status(
        _authed(
            {
                "params": {
                    "call_id": "sw-1",
                    "call_state": "ended",
                    "end_reason": "hangup",
                    "end_source": end_source,
                }
            }
        )
    )
    assert status_update.call_args.kwargs["status"] == expected


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


@pytest.mark.parametrize(
    "connect_state,failed_reason,expected",
    [
        ("failed", "no_answer", "no-answer"),
        ("failed", "busy", "busy"),
        ("failed", "not_found", "failed"),
        ("connected", "", "in-progress"),
    ],
)
async def test_connect_status_records_provider_result(
    status_update, connect_state, failed_reason, expected
):
    response = await handle_sw_connect_status(
        _authed(
            {
                "event_type": "calling.call.connect",
                "params": {
                    "call_id": "sw-1",
                    "connect_state": connect_state,
                    "failed_reason": failed_reason,
                },
            },
            query={"call_id": "sw-1"},
        )
    )
    assert response.status_code == 200
    assert status_update.call_args.kwargs["parent_call_sid"] == "sw-1"
    assert status_update.call_args.kwargs["status"] == expected


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
    assert "/telephony/sw-connect-status" in paths
    assert "/telephony/sw-recording" in paths


# ── Inbound ──────────────────────────────────────────────────────────────────────


async def test_dialer_connect_joins_a_conference_instead_of_dialling():
    """A rep accepting an inbound call has no lead number to dial.

    The conference branch must be checked before the lead lookup: falling through would
    hit the "no dialable lead number" hangup and drop a caller who is already on the line.
    """
    with (
        patch(f"{_MODULE}._rep_supabase_id", new=AsyncMock(return_value="rep-user")),
        patch(f"{_MODULE}.is_claimed_inbound_conference", new=AsyncMock(return_value=True)) as claim,
        patch(f"{_MODULE}.bind_inbound_rep_leg", new=AsyncMock(return_value=True)) as bind,
    ):
        response = await handle_sw_dialer_connect(
            _authed({"call_id": "rep-browser", "vars": {"userVariables": {"rep": "rep-1", "conference": "inbound-abc"}}})
        )
    claim.assert_awaited_once_with(conference_name="inbound-abc", rep_user_id="rep-user")
    bind.assert_awaited_once_with(
        conference_name="inbound-abc", rep_user_id="rep-user",
        provider="signalwire", rep_call_id="rep-browser",
    )
    doc = _payload_of(response)

    # join_conference, not join_room: join_room 500'd on SignalWire's side ~100s into a
    # real call (see build_inbound_hold_swml). The rep is the main participant.
    assert doc["sections"]["main"] == [
        {"join_conference": {"name": "inbound-abc", "start_on_enter": True, "end_on_exit": True}}
    ]


async def test_signalwire_cannot_join_someone_elses_conference_by_name():
    with (
        patch(f"{_MODULE}._rep_supabase_id", new=AsyncMock(return_value="different-rep")),
        patch(f"{_MODULE}.is_claimed_inbound_conference", new=AsyncMock(return_value=False)),
    ):
        response = await handle_sw_dialer_connect(_authed({
            "vars": {"userVariables": {"rep": "rep-2", "conference": "sysevo-overflow-private"}}
        }))
    assert _is_hangup(_payload_of(response))


async def test_dialer_connect_still_dials_when_no_conference_is_given():
    """Agent/campaign legs without a rep identity remain supported."""
    with (
        patch(f"{_MODULE}.create_dialer_call", new=AsyncMock()),
        patch(f"{_MODULE}._rep_supabase_id", new=AsyncMock(return_value=None)),
        patch(f"{_MODULE}.get_backend_endpoints", new=AsyncMock(return_value=("", ""))),
    ):
        response = await handle_sw_dialer_connect(
            _authed({"vars": {"userVariables": {"lead": "+15550001111"}}})
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
    assert {
        "join_conference": {"name": "inbound-abc", "start_on_enter": False, "end_on_exit": False}
    } in doc["sections"]["main"]
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
    # join_conference is what a held caller would be put into; checking for the old
    # join_room here passed vacuously, since nothing emits it any more.
    assert not any("join_conference" in step or "join_room" in step for step in steps), (
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
    assert connect["timeout"] == 60


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
        connect_status_webhook="https://api.example.com/sw-connect-status?call_id=abc",
    )
    connect = next(s["connect"] for s in doc["sections"]["main"] if "connect" in s)
    assert "answer_on_bridge" not in connect
    # SignalWire's from_name property only applies to SIP calls, not PSTN.
    assert "from_name" not in connect
    assert connect["call_state_url"].endswith("call_id=abc")
    assert connect["status_url"].endswith("call_id=abc")
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
    assert any("join_conference" in s for s in steps)
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

    assert any("join_conference" in s for s in _payload_of(response)["sections"]["main"])


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


# --------------------------------------------------------------------------
# Who hung up (end_source), calibrated 2026-09-25.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "end_source,expected",
    [("outbound", "rep"), ("inbound", "prospect"), ("INBOUND", "prospect"), ("", None), ("weird", None)],
)
async def test_status_records_which_side_hung_up(status_update, end_source, expected):
    # The real payload shape from the 20:13 call reps reported as cut off.
    body = {"params": {"call_id": "far-leg", "call_state": "ended", "end_reason": "hangup", "end_source": end_source}}
    await handle_sw_call_status(_request(body, query={"k": _SECRET, "call_id": "sw-1"}))
    assert status_update.call_args.kwargs["ended_by"] == expected


@pytest.mark.parametrize(
    "end_reason,expected_status",
    [
        ("noAnswer", "no-answer"),
        ("no_answer", "no-answer"),
        ("NO-ANSWER", "no-answer"),
        ("busy", "busy"),
        ("declined", "canceled"),
        ("decline", "canceled"),
        ("abandoned", "canceled"),
        ("not_found", "failed"),
        ("max_duration", "completed"),
        ("cancel", "canceled"),
        ("error", "failed"),
    ],
)
async def test_terminal_reason_aliases_do_not_turn_ringouts_into_completed(
    status_update, end_reason, expected_status
):
    body = {
        "params": {
            "call_id": "far-leg",
            "call_state": "ended",
            "end_reason": end_reason,
            "end_source": "outbound",
            "duration": 55,
        }
    }
    await handle_sw_call_status(_request(body, query={"k": _SECRET, "call_id": "sw-1"}))
    assert status_update.call_args.kwargs["status"] == expected_status
    assert status_update.call_args.kwargs["ended_by"] is None


async def test_end_source_is_ignored_before_the_call_ends(status_update):
    body = {"params": {"call_id": "far-leg", "call_state": "answered", "end_source": "inbound"}}
    await handle_sw_call_status(_request(body, query={"k": _SECRET, "call_id": "sw-1"}))
    assert status_update.call_args.kwargs["ended_by"] is None

def test_signalwire_parallel_overflow_gets_a_private_waiting_room():
    """A second lead must never be directed into the winner's live conference."""
    from api.services.telephony.dialer.swml import build_signalwire_parallel_overflow_swml

    document = build_signalwire_parallel_overflow_swml(
        conference_name="sysevo-overflow-session-leg"
    )
    join = document["sections"]["main"][0]["join_conference"]
    assert join == {
        "name": "sysevo-overflow-session-leg",
        "start_on_enter": False,
        "end_on_exit": False,
    }


async def test_signalwire_second_human_routes_to_reserved_teammate():
    """The callback returns an isolated conference only after durable routing succeeds."""
    from api.services.telephony.dialer.signalwire_routes import handle_sw_parallel_decision

    session_id = "00000000-0000-0000-0000-000000000001"
    leg_id = "00000000-0000-0000-0000-000000000002"
    routed = {
        "inbound_call_id": "00000000-0000-0000-0000-000000000003",
        "rep_user_id": "00000000-0000-0000-0000-000000000004",
        "caller_name": "Example Co",
        "conference_name": f"sysevo-overflow-{leg_id}",
    }

    def _close_coro(coro):
        coro.close()

    with (
        patch(f"{_MODULE}._secret_ok", return_value=True),
        patch(f"{_MODULE}.signalwire_parallel_store.get_leg_context", new=AsyncMock(return_value={
            "session": {"id": session_id, "rep_user_id": routed["rep_user_id"], "rep_call_id": "rep-call", "conference_name": f"sysevo-par-{session_id}", "status": "connected"},
            "leg": {"id": leg_id, "entry_id": "entry", "provider_call_id": "prospect-call", "status": "human"},
        })),
        patch(f"{_MODULE}.signalwire_parallel_store.update_leg", new=AsyncMock(return_value=True)),
        patch(f"{_MODULE}.signalwire_parallel_store.claim_winner", new=AsyncMock(return_value=False)),
        patch(f"{_MODULE}.signalwire_parallel_store.route_to_teammate", new=AsyncMock(return_value=routed)) as route,
        patch(f"{_MODULE}.notify_inbound_call") as notify,
        patch(f"{_MODULE}.get_backend_endpoints", new=AsyncMock(return_value=("https://api.example.com", "wss://api.example.com"))),
        patch(f"{_MODULE}._spawn_background", side_effect=_close_coro),
        patch(f"{_MODULE}.update_dialer_call_status", new=AsyncMock()) as update_call,
    ):
        response = await handle_sw_parallel_decision(_authed(
            {"call": {"call_id": "prospect-call", "amd_result": "human", "amd_ms": 2400}},
            query={"session_id": session_id, "leg_id": leg_id},
        ))

    route.assert_awaited_once()
    notify.assert_called_once_with(routed["inbound_call_id"])
    update_call.assert_awaited_once()
    assert update_call.await_args.kwargs["status"] == "in-progress"
    join = _payload_of(response)["sections"]["main"][0]["join_conference"]
    assert join["name"] == routed["conference_name"]
    assert join["name"] != f"sysevo-par-{session_id}"
    assert "sw-overflow-conference" in join["status_callback"]
    assert join["status_callback_event"] == "join leave"


async def test_signalwire_parallel_leg_still_runs_amd_after_first_human_connects():
    """A late second answer must reach AMD and then the overflow decision route."""
    from api.services.telephony.dialer.signalwire_routes import handle_sw_parallel_leg

    session_id = "00000000-0000-0000-0000-000000000021"
    leg_id = "00000000-0000-0000-0000-000000000022"
    provider_call_id = "prospect-second-call"
    context = {
        "session": {
            "id": session_id,
            "rep_user_id": "00000000-0000-0000-0000-000000000004",
            "rep_call_id": "rep-browser-call",
            "conference_name": f"sysevo-par-{session_id}",
            "status": "connected",
            "winner_leg_id": "00000000-0000-0000-0000-000000000020",
        },
        "leg": {
            "id": leg_id,
            "entry_id": "00000000-0000-0000-0000-000000000023",
            "provider_call_id": provider_call_id,
            "status": "ringing",
        },
    }

    with (
        patch(f"{_MODULE}.signalwire_parallel_store.get_leg_context", new=AsyncMock(return_value=context)),
        patch(f"{_MODULE}.signalwire_parallel_store.update_leg", new=AsyncMock(return_value=True)),
        patch(f"{_MODULE}.signalwire_parallel_store.get_voicemail_policy", new=AsyncMock(return_value={
            "machine_detection_enabled": True,
            "machine_detection_preset": "balanced",
        })),
        patch(f"{_MODULE}.get_backend_endpoints", new=AsyncMock(return_value=(
            "https://api.sysevo.io", "wss://api.sysevo.io"
        ))),
    ):
        response = await handle_sw_parallel_leg(_authed(
            {"call": {"call_id": provider_call_id}},
            query={"session_id": session_id, "leg_id": leg_id},
        ))

    document = _payload_of(response)
    steps = document["sections"]["main"]
    assert steps[0]["detect_machine"]["wait"] is True
    assert "sw-parallel-decide" in steps[1]["execute"]["dest"]


@pytest.mark.parametrize("line_count", [1, 3])
async def test_parallel_always_screens_even_when_single_call_amd_is_disabled(line_count):
    from api.services.telephony.dialer.signalwire_routes import handle_sw_parallel_leg

    session_id = "00000000-0000-0000-0000-000000000021"
    leg_id = "00000000-0000-0000-0000-000000000022"
    with (
        patch(f"{_MODULE}.signalwire_parallel_store.get_leg_context", new=AsyncMock(return_value={
            "session": {"status": "dialing", "rep_user_id": "rep", "rep_call_id": "browser", "line_count": line_count},
            "leg": {"provider_call_id": "prospect-call", "entry_id": "entry", "status": "screening"},
        })),
        patch(f"{_MODULE}.signalwire_parallel_store.get_voicemail_policy", new=AsyncMock(return_value={
            "machine_detection_enabled": False,
        })),
        patch(f"{_MODULE}.get_backend_endpoints", new=AsyncMock(return_value=("https://api.example.test", ""))),
    ):
        response = await handle_sw_parallel_leg(_authed(
            {"call": {"call_id": "prospect-call"}},
            query={"session_id": session_id, "leg_id": leg_id},
        ))
    steps = _payload_of(response)["sections"]["main"]
    assert any("detect_machine" in step for step in steps) is (line_count > 1)


@pytest.mark.parametrize("line_count", [1, 3])
async def test_machine_pass_to_rep_is_only_available_for_single_screened_calls(line_count):
    from api.services.telephony.dialer.signalwire_routes import handle_sw_parallel_decision

    session_id = "00000000-0000-0000-0000-000000000021"
    leg_id = "00000000-0000-0000-0000-000000000022"
    with (
        patch(f"{_MODULE}.signalwire_parallel_store.get_leg_context", new=AsyncMock(return_value={
            "session": {"status": "dialing", "rep_user_id": "rep", "rep_call_id": "browser", "line_count": line_count,
                        "conference_name": f"sysevo-par-{session_id}"},
            "leg": {"provider_call_id": "prospect-call", "entry_id": "entry", "status": "screening"},
        })),
        patch(f"{_MODULE}.signalwire_parallel_store.update_leg", new=AsyncMock(return_value=True)),
        patch(f"{_MODULE}.signalwire_parallel_store.get_voicemail_policy", new=AsyncMock(return_value={"on_machine": "pass_to_rep"})),
        patch(f"{_MODULE}.signalwire_parallel_store.claim_winner", new=AsyncMock(return_value=True)) as winner,
        patch(f"{_MODULE}.signalwire_parallel_store.record_outcome", new=AsyncMock(return_value=True)) as outcome,
        patch(f"{_MODULE}._load_signalwire_parallel_entries", new=AsyncMock(return_value=[{"id": "entry", "phone": "+14155550123"}])),
        patch(f"{_MODULE}.update_signalwire_dialer_amd", new=AsyncMock()),
        patch(f"{_MODULE}.get_backend_endpoints", new=AsyncMock(return_value=("https://api.example.test", ""))),
    ):
        response = await handle_sw_parallel_decision(_authed(
            {"call": {"call_id": "prospect-call", "amd_result": "machine"}},
            query={"session_id": session_id, "leg_id": leg_id},
        ))
    if line_count == 1:
        winner.assert_awaited_once()
        outcome.assert_not_awaited()
        assert "join_conference" in _payload_of(response)["sections"]["main"][0]
    else:
        winner.assert_not_awaited()
        outcome.assert_awaited_once()
        assert _is_hangup(_payload_of(response))


async def test_signalwire_parallel_caller_ids_are_resolved_in_one_batch():
    from api.services.telephony.dialer.signalwire_routes import _resolve_signalwire_parallel_caller_ids

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = [
        {"entry_id": "lead-a", "phone_number": "+14155550101", "provider": "signalwire"},
        {"entry_id": "lead-b", "phone_number": "+14155550102", "provider": "signalwire"},
    ]
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch(f"{_MODULE}.SUPABASE_URL", "https://db.example.supabase.co"),
        patch(f"{_MODULE}.SUPABASE_SERVICE_ROLE_KEY", "service-key"),
        patch(f"{_MODULE}.httpx.AsyncClient", return_value=client),
        patch(f"{_MODULE}._is_signalwire_owned_number", new=AsyncMock(return_value=True)) as owns,
    ):
        resolved = await _resolve_signalwire_parallel_caller_ids(
            rep_user_id="rep-user",
            entry_ids=["lead-a", "lead-b"],
            fallback_number="+12095550100",
        )

    assert resolved == {"lead-a": "+14155550101", "lead-b": "+14155550102"}
    client.post.assert_awaited_once()
    request = client.post.await_args
    assert request.args[0].endswith("/rpc/dialer_resolve_parallel_caller_ids")
    assert request.kwargs["json"]["p_entry_ids"] == ["lead-a", "lead-b"]
    assert request.kwargs["json"]["p_provider"] == "signalwire"
    assert owns.await_count == 2


@pytest.mark.parametrize("call_state,should_close", [("answered", False), ("ended", True)])
async def test_stale_signalwire_answer_status_cannot_resurrect_routed_leg(call_state, should_close):
    """A delayed provider answer event must not overwrite a routed/terminal leg."""
    from api.services.telephony.dialer.signalwire_routes import handle_sw_parallel_status

    session_id = "00000000-0000-0000-0000-000000000011"
    leg_id = "00000000-0000-0000-0000-000000000012"
    with (
        patch(f"{_MODULE}._secret_ok", return_value=True),
        patch(f"{_MODULE}.signalwire_parallel_store.get_leg_context", new=AsyncMock(return_value={
            "session": {"id": session_id, "rep_user_id": "rep", "status": "connected"},
            "leg": {"id": leg_id, "provider_call_id": "prospect-call", "status": "completed"},
        })),
        patch(f"{_MODULE}.signalwire_parallel_store.update_leg", new=AsyncMock(return_value=False)),
        patch(f"{_MODULE}.update_dialer_call_status", new=AsyncMock()) as update_call,
        patch(f"{_MODULE}.close_inbound_call", new=AsyncMock()) as close_inbound,
        patch(f"{_MODULE}.evaluate_overflow_guard", new=AsyncMock()) as evaluate,
    ):
        response = await handle_sw_parallel_status(_authed(
            {"call": {"call_id": "prospect-call", "call_state": call_state}},
            query={"session_id": session_id, "leg_id": leg_id},
        ))

    assert _payload_of(response) == {"ok": True, "ignored": True, "reason": "stale_state"}
    if should_close:
        update_call.assert_awaited_once()
        assert update_call.await_args.kwargs["status"] == "completed"
        close_inbound.assert_awaited_once_with(provider_call_id="prospect-call")
        evaluate.assert_awaited_once_with(provider_call_id="prospect-call", provider="signalwire")
    else:
        update_call.assert_not_awaited()
        close_inbound.assert_not_awaited()
        evaluate.assert_not_awaited()


async def test_screened_single_signalwire_queue_call_uses_one_reserved_prospect_leg():
    """Single queue dialing must use AMD without weakening Parallel's line contract."""
    from api.services.telephony.dialer.signalwire_routes import _start_signalwire_session

    rep_id = "00000000-0000-0000-0000-000000000021"
    entry_id = "00000000-0000-0000-0000-000000000022"
    leg_id = "00000000-0000-0000-0000-000000000023"
    provider_call_id = "00000000-0000-0000-0000-000000000024"
    conference_name = "sysevo-par-00000000-0000-0000-0000-000000000025"

    with (
        patch("api.services.telephony.dialer.parallel_auth.verify_parallel_auth_token", return_value=rep_id),
        patch(f"{_MODULE}._rep_supabase_id", new=AsyncMock(return_value=rep_id)),
        patch(f"{_MODULE}._resolve_signalwire_caller_id", new=AsyncMock(return_value="+12095550123")),
        patch(f"{_MODULE}.get_backend_endpoints", new=AsyncMock(return_value=("https://api.example.com", ""))),
        patch(f"{_MODULE}._load_signalwire_parallel_entries", new=AsyncMock(return_value=[{"id": entry_id, "phone": "+14155550123"}])),
        patch(f"{_MODULE}.signalwire_parallel_store.start_screened_single_session", new=AsyncMock(return_value=[{"id": leg_id}] )) as reserve,
        patch(f"{_MODULE}._resolve_signalwire_parallel_caller_id", new=AsyncMock(return_value="+12095550123")),
        patch(f"{_MODULE}._check_signalwire_parallel_compliance", new=AsyncMock()),
        patch(f"{_MODULE}.create_dialer_call", new=AsyncMock(return_value=True)),
        patch("api.services.telephony.dialer.signalwire_calling.originate_call", new=AsyncMock(return_value={"id": provider_call_id})) as originate,
        patch(f"{_MODULE}.signalwire_parallel_store.register_provider_call", new=AsyncMock(return_value=True)),
    ):
        response = await _start_signalwire_session(
            request=_authed({}),
            payload={},
            query={
                "ParallelAuthToken": "short-lived-token",
                "EntryId": entry_id,
                "RepId": rep_id,
                "CallSid": "browser-call-0001",
            },
            identity=rep_id,
            screened_single=True,
        )

    reserve.assert_awaited_once_with(
        session_id=reserve.await_args.kwargs["session_id"],
        rep_user_id=rep_id,
        rep_call_id="browser-call-0001",
        conference_name=reserve.await_args.kwargs["conference_name"],
        entry_id=entry_id,
    )
    originate.assert_awaited_once()
    assert _payload_of(response)["sections"]["main"][0]["join_conference"]["name"].startswith("sysevo-par-")


async def test_signalwire_parallel_originates_every_selected_lead_with_batch_caller_ids():
    """The server must fan out N accepted PSTN legs with the N selected leads."""
    from api.services.telephony.dialer.signalwire_routes import _start_signalwire_session

    rep_id = "00000000-0000-0000-0000-000000000031"
    entry_ids = [
        "00000000-0000-0000-0000-000000000032",
        "00000000-0000-0000-0000-000000000033",
    ]
    entries = [
        {"id": entry_ids[0], "phone": "+14155550101"},
        {"id": entry_ids[1], "phone": "+16025550102"},
    ]
    caller_ids = {
        entry_ids[0]: "+14155550991",
        entry_ids[1]: "+16025550992",
    }
    originate = AsyncMock(side_effect=[{"id": "prospect-call-1"}, {"id": "prospect-call-2"}])

    with (
        patch("api.services.telephony.dialer.parallel_auth.verify_parallel_auth_token", return_value=rep_id),
        patch(f"{_MODULE}._rep_supabase_id", new=AsyncMock(return_value=rep_id)),
        patch(f"{_MODULE}._resolve_signalwire_caller_id", new=AsyncMock(return_value="+12095550100")),
        patch(f"{_MODULE}.get_backend_endpoints", new=AsyncMock(return_value=("https://api.example.com", ""))),
        patch(f"{_MODULE}._load_signalwire_parallel_entries", new=AsyncMock(return_value=entries)),
        patch(f"{_MODULE}.signalwire_parallel_store.start_session", new=AsyncMock(return_value=[
            {"id": "leg-one"}, {"id": "leg-two"},
        ])) as reserve,
        patch(f"{_MODULE}._resolve_signalwire_parallel_caller_ids", new=AsyncMock(return_value=caller_ids)) as resolve_batch,
        patch(f"{_MODULE}._check_signalwire_parallel_compliance", new=AsyncMock()) as compliance,
        patch(f"{_MODULE}.create_dialer_call", new=AsyncMock(return_value=True)) as save_call,
        patch("api.services.telephony.dialer.signalwire_calling.originate_call", new=originate),
        patch(f"{_MODULE}.signalwire_parallel_store.register_provider_call", new=AsyncMock(return_value=True)),
    ):
        response = await _start_signalwire_session(
            request=_authed({}),
            payload={},
            query={
                "ParallelAuthToken": "short-lived-token",
                "ParallelEntryIds": json.dumps(entry_ids),
                "ParallelLineCount": "2",
                "CallSid": "browser-call-0002",
            },
            identity=rep_id,
        )

    reserve.assert_awaited_once()
    resolve_batch.assert_awaited_once_with(
        rep_user_id=rep_id, entry_ids=entry_ids, fallback_number="+12095550100"
    )
    assert compliance.await_count == 2
    assert save_call.await_count == 3  # one parent history row plus both lead rows
    assert originate.await_count == 2
    originated = {
        call.kwargs["to_number"]: call.kwargs["from_number"]
        for call in originate.await_args_list
    }
    assert originated == {
        "+14155550101": caller_ids[entry_ids[0]],
        "+16025550102": caller_ids[entry_ids[1]],
    }
    assert _payload_of(response)["sections"]["main"][0]["join_conference"]["name"].startswith("sysevo-par-")


async def test_screened_single_mode_dispatches_to_server_amd_flow():
    expected = {"ok": True}
    with patch(f"{_MODULE}._start_signalwire_session", new=AsyncMock(return_value=expected)) as start:
        response = await handle_sw_dialer_connect(_authed(
            {"params": {"rep": "rep-user", "DialMode": "screened_single"}}
        ))

    assert response is expected
    start.assert_awaited_once()
    assert start.await_args.kwargs["screened_single"] is True
