"""Shared TwiML attributes for every participant in a dialer conference."""
from xml.sax.saxutils import quoteattr


def conference_telemetry_attributes(*, events_url: str, recording_url: str) -> str:
    """The conference-level attributes that must be identical on EVERY leg.

    Twilio applies conference-level attributes only from the TwiML of the
    participant that CREATES the conference, and ignores them on everyone
    who joins afterwards. The rep normally creates it - calls.create returns
    as soon as the lead's call is queued, and the lead still has to ring -
    but "normally" is a race, not a guarantee: an instantly-answering
    voicemail, a SIP endpoint, or an unusually slow rep leg can put the lead
    in first. If that happens and only the rep's leg carried these, the
    conference comes up with no recording, no recording callback and no
    conference-start event at all - which also leaves conference_sid NULL,
    so update_dialer_call_recording_by_conference_sid would have nothing to
    match even if a recording did appear. So both legs carry them, from this
    one definition, and whichever leg wins the race the conference is
    recorded and reporting.

    Repeating them cannot double-record: recording is a property of the
    conference, not of a participant, so only the creator's copy takes
    effect and the loser's is discarded.

    Returns "" (attributes omitted entirely) unless BOTH URLs are absolute.
    That is the guard for an unresolved backend endpoint, which yields a
    relative "/api/v1/..." rather than an empty string: Twilio rejects a
    relative callback URL, and a rejected TwiML document costs the whole
    leg - silence for the lead - which is strictly worse than a conference
    that merely isn't reporting.
    """
    if not events_url.startswith(("http://", "https://")):
        return ""
    if not recording_url.startswith(("http://", "https://")):
        return ""
    return (
        f"statusCallback={quoteattr(events_url)} "
        'statusCallbackEvent="start end join leave" '
        'record="record-from-start" '
        f"recordingStatusCallback={quoteattr(recording_url)} "
    )
