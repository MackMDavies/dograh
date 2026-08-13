"""Unit tests for SWML generation. SWML is JSON - assert on parsed structure."""
from api.services.telephony.dialer.swml import build_dialer_swml, build_hangup_swml


def test_build_dialer_swml_connects_to_lead_with_caller_id():
    doc = build_dialer_swml(
        lead_number="+15559876543",
        caller_id="+15551234567",
        recording_webhook="https://api.example.com/api/v1/telephony/sw-recording",
    )
    steps = doc["sections"]["main"]
    connect = next(s["connect"] for s in steps if "connect" in s)
    assert connect["to"] == "+15559876543"
    assert connect["from"] == "+15551234567"


def test_build_dialer_swml_records_before_connecting():
    doc = build_dialer_swml(
        lead_number="+15559876543",
        caller_id="+15551234567",
        recording_webhook="https://api.example.com/api/v1/telephony/sw-recording",
    )
    steps = doc["sections"]["main"]
    keys = [k for s in steps for k in s]
    # record_call must come first, or the opening of the call is lost.
    assert keys.index("record_call") < keys.index("connect")


def test_build_dialer_swml_sets_the_recording_webhook():
    doc = build_dialer_swml(
        lead_number="+15559876543",
        caller_id="+15551234567",
        recording_webhook="https://api.example.com/api/v1/telephony/sw-recording",
    )
    rec = next(s["record_call"] for s in doc["sections"]["main"] if "record_call" in s)
    assert rec["status_url"] == "https://api.example.com/api/v1/telephony/sw-recording"


def test_build_dialer_swml_omits_recording_when_no_webhook():
    doc = build_dialer_swml(
        lead_number="+15559876543", caller_id="+15551234567", recording_webhook=""
    )
    keys = [k for s in doc["sections"]["main"] for k in s]
    # Better a call with no recording than SWML pointing at a bad URL.
    assert "record_call" not in keys
    assert "connect" in keys


def test_build_hangup_swml_is_valid_and_only_hangs_up():
    doc = build_hangup_swml()
    assert doc["sections"]["main"] == [{"hangup": {}}]


def test_swml_is_json_serialisable():
    import json

    json.dumps(
        build_dialer_swml(
            lead_number="+1555", caller_id="+1666", recording_webhook="https://x/y"
        )
    )
