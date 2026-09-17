"""Unit tests for SWML generation. SWML is JSON - assert on parsed structure."""
from api.services.telephony.dialer.swml import (
    build_conference_join_swml,
    build_dialer_swml,
    build_hangup_swml,
    build_inbound_hold_swml,
)


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


def test_a_long_call_is_never_cut_short_by_the_script():
    """A rep on a good call can be on it for half an hour.

    `timeout` bounds the RING, not the conversation, and the two get confused:
    when reps reported being cut off mid-call it was the first thing suspected.
    Guard both facts so neither can be quietly changed into a cap — and state
    max_duration rather than inheriting whatever SignalWire's default happens to
    be, so a thirty-minute call cannot depend on a number nobody here verified.
    """
    doc = build_dialer_swml(
        lead_number="+15559876543",
        caller_id="+15551234567",
        recording_webhook="https://api.example.com/api/v1/telephony/sw-recording",
    )
    connect = next(s["connect"] for s in doc["sections"]["main"] if "connect" in s)
    # Ring timeout: short on purpose, and NOT a limit on the conversation.
    assert connect["timeout"] == 30
    # Conversation length: stated, and far beyond any real sales call.
    assert connect["max_duration"] >= 4 * 60 * 60


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


def test_spoken_company_name_is_not_the_written_spelling():
    """Our own name is spelled for the ear here, not for the eye.

    "Sysevo" is not a word, so TTS reads it letter-pattern-wise and says
    "Sys-AY-vo" - every caller who reached the hold greeting heard the company
    introduce itself by the wrong name. This guards the respelling against being
    tidied back, which is exactly what it looks like it needs.
    """
    from api.services.telephony.dialer.swml import SPOKEN_COMPANY_NAME

    assert "Sysevo" not in SPOKEN_COMPANY_NAME
    # Still recognisably us, so a mistyped constant cannot pass silently.
    assert SPOKEN_COMPANY_NAME.lower().startswith("sis")
    assert "ee" in SPOKEN_COMPANY_NAME.lower()


def test_inbound_greeting_is_never_read_out_as_the_written_spelling():
    from api.services.telephony.dialer.swml import (
        SPOKEN_COMPANY_NAME,
        build_inbound_hold_swml,
    )

    doc = build_inbound_hold_swml(
        conference_name="inbound-abc",
        recording_webhook="",
        greeting=f"Thanks for calling {SPOKEN_COMPANY_NAME}.",
    )
    spoken = [
        s["play"]["url"] for s in doc["sections"]["main"] if "play" in s
    ]
    assert spoken, "the greeting must actually be spoken"
    for line in spoken:
        assert "Sysevo" not in line

# ── Inbound hold: a conference, not a video room ──────────────────────────
#
# join_room fails here in a way that only shows up on a live call — see the
# SignalWire voice log quoted in build_inbound_hold_swml. These pin the verb and
# the two flags that decide who starts and ends the room.


def test_inbound_hold_uses_a_conference_not_a_video_room():
    doc = build_inbound_hold_swml(
        conference_name="inbound-abc123",
        recording_webhook="https://api.example.com/api/v1/telephony/sw-recording",
    )
    steps = doc["sections"]["main"]
    assert not any("join_room" in s for s in steps), "join_room joins a VIDEO room"
    conference = next(s["join_conference"] for s in steps if "join_conference" in s)
    assert conference["name"] == "inbound-abc123"


def test_waiting_caller_does_not_start_the_conference():
    # Starting it on the caller's arrival puts them alone in a live room, which is
    # silence rather than hold treatment.
    doc = build_inbound_hold_swml(
        conference_name="inbound-abc123",
        recording_webhook="https://api.example.com/api/v1/telephony/sw-recording",
    )
    conference = next(
        s["join_conference"] for s in doc["sections"]["main"] if "join_conference" in s
    )
    assert conference["start_on_enter"] is False


def test_waiting_caller_hanging_up_does_not_tear_down_the_conference():
    # A rep may be halfway into joining it.
    doc = build_inbound_hold_swml(
        conference_name="inbound-abc123",
        recording_webhook="https://api.example.com/api/v1/telephony/sw-recording",
    )
    conference = next(
        s["join_conference"] for s in doc["sections"]["main"] if "join_conference" in s
    )
    assert conference["end_on_exit"] is False


def test_inbound_hold_still_records_before_joining():
    # The opening seconds, including the greeting, are captured on the caller's leg.
    doc = build_inbound_hold_swml(
        conference_name="inbound-abc123",
        recording_webhook="https://api.example.com/api/v1/telephony/sw-recording",
    )
    steps = doc["sections"]["main"]
    record_at = next(i for i, s in enumerate(steps) if "record_call" in s)
    join_at = next(i for i, s in enumerate(steps) if "join_conference" in s)
    assert record_at < join_at


def test_answering_rep_starts_and_ends_the_conference():
    # The rep is the main participant. Without end_on_exit the caller is left alone
    # in a room after the rep hangs up, with nothing saying the call is over.
    doc = build_conference_join_swml(conference_name="inbound-abc123")
    steps = doc["sections"]["main"]
    assert not any("join_room" in s for s in steps)
    conference = next(s["join_conference"] for s in steps if "join_conference" in s)
    assert conference["name"] == "inbound-abc123"
    assert conference["start_on_enter"] is True
    assert conference["end_on_exit"] is True


def test_answering_rep_does_not_record_a_second_copy():
    # The caller's leg is already recording. A second recorder bills twice for one
    # conversation and produces two files to reconcile.
    doc = build_conference_join_swml(conference_name="inbound-abc123")
    assert not any("record_call" in s for s in doc["sections"]["main"])


# ── Live audio taps ──────────────────────────────────────────────────────────
#
# The tap is what makes a call listenable while it is happening. It is additive:
# it must never change how the call is bridged, because that path took two
# outages to get working.


def test_outbound_taps_both_directions():
    # `direction` defaults to `speak`, which is only our side. A manager monitoring
    # a call needs to hear the prospect too.
    doc = build_dialer_swml(
        lead_number="+15559876543",
        caller_id="+15551234567",
        recording_webhook="https://api.example.com/api/v1/telephony/sw-recording",
        tap_websocket="wss://api.example.com/api/v1/telephony/sw-tap?call_id=abc",
    )
    tap = next(s["tap"] for s in doc["sections"]["main"] if "tap" in s)
    assert tap["uri"] == "wss://api.example.com/api/v1/telephony/sw-tap?call_id=abc"
    assert tap["direction"] == "both"


def test_inbound_taps_the_caller_leg():
    doc = build_inbound_hold_swml(
        conference_name="inbound-abc123",
        recording_webhook="",
        tap_websocket="wss://api.example.com/tap",
    )
    steps = doc["sections"]["main"]
    assert any("tap" in s for s in steps)
    # Before the join, for the same reason recording is: the greeting and the wait
    # are part of the call.
    assert next(i for i, s in enumerate(steps) if "tap" in s) < next(
        i for i, s in enumerate(steps) if "join_conference" in s
    )


def test_tap_starts_before_the_bridge():
    # A tap started after the connect would miss the opening of the conversation,
    # which is the part worth hearing.
    doc = build_dialer_swml(
        lead_number="+15559876543",
        caller_id="+15551234567",
        recording_webhook="",
        tap_websocket="wss://api.example.com/tap",
    )
    steps = doc["sections"]["main"]
    assert next(i for i, s in enumerate(steps) if "tap" in s) < next(
        i for i, s in enumerate(steps) if "connect" in s
    )


def test_no_tap_url_means_no_tap_step():
    # Monitoring is worth having; it is not worth a malformed SWML document on a
    # live call. No endpoint, no tap, call proceeds exactly as before.
    doc = build_dialer_swml(
        lead_number="+15559876543",
        caller_id="+15551234567",
        recording_webhook="",
    )
    assert not any("tap" in s for s in doc["sections"]["main"])
    assert any("connect" in s for s in doc["sections"]["main"])


def test_tap_does_not_disturb_the_connect():
    # The whole justification for a tap over a conference: the bridge is untouched.
    without = build_dialer_swml(
        lead_number="+15559876543", caller_id="+15551234567", recording_webhook="",
    )
    with_tap = build_dialer_swml(
        lead_number="+15559876543", caller_id="+15551234567", recording_webhook="",
        tap_websocket="wss://api.example.com/tap",
    )
    connect_of = lambda d: next(s["connect"] for s in d["sections"]["main"] if "connect" in s)
    assert connect_of(without) == connect_of(with_tap)
