"""SWML documents for the sales-rep dialer.

SWML is JSON, so these are built as dicts and serialised by the framework -
never string-concatenated. That removes the whole class of escaping bugs the
Twilio TwiML path needed xml.sax.saxutils to defend against.

SCOPE: dialer only. Campaigns and agent calls do not use SWML.
"""


def build_hangup_swml() -> dict:
    """Refuse the call politely. Used for bad signatures and bad input."""
    return {"sections": {"main": [{"hangup": {}}]}}


def build_dialer_swml(
    *, lead_number: str, caller_id: str, recording_webhook: str
) -> dict:
    """Record the call, then bridge the rep to the lead.

    Order matters: record_call precedes connect so the opening seconds are
    captured. Unlike Twilio, where recording was an attribute of the call
    topology (and so had to be correlated back by ConferenceSid), SWML
    records independently - the recording webhook can carry our own call id.

    An empty recording_webhook omits recording entirely rather than emitting
    SWML that points somewhere useless: a call without a recording beats a
    call that fails to connect.
    """
    steps: list[dict] = []
    if recording_webhook:
        steps.append(
            {
                "record_call": {
                    "stereo": True,
                    "format": "mp3",
                    "status_url": recording_webhook,
                }
            }
        )
    steps.append({"connect": {"to": lead_number, "from": caller_id}})
    return {"sections": {"main": steps}}
