"""SWML documents for the sales-rep dialer.

SWML is JSON, so these are built as dicts and serialised by the framework -
never string-concatenated. That removes the whole class of escaping bugs the
Twilio TwiML path needed xml.sax.saxutils to defend against.

SCOPE: dialer only. Campaigns and agent calls do not use SWML.
"""


def build_hangup_swml() -> dict:
    """Refuse the call politely. Used for bad signatures and bad input."""
    return {"sections": {"main": [{"hangup": {}}]}}


def _tap_step(tap_websocket: str) -> dict | None:
    """Fork the call's audio to our WebSocket, so it can be listened to live.

    Both directions: a manager monitoring a call needs to hear the prospect as well as
    the rep, and `direction` defaults to `speak`, which is only our side.

    Additive, like record_call, and that is the point. Monitoring by conference would
    mean originating the lead into a room instead of bridging with connect, which is a
    rewrite of the path that took two outages to get working. A tap leaves the call
    exactly as it is.

    https://signalwire.com/docs/swml/reference/calling/tap
    """
    if not tap_websocket:
        return None
    return {"tap": {"uri": tap_websocket, "direction": "both"}}


def build_dialer_swml(
    *,
    lead_number: str,
    caller_id: str,
    recording_webhook: str,
    call_state_webhook: str = "",
    tap_websocket: str = "",
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
    # Before the connect, like the recording: a tap started after the bridge would miss
    # the opening of the conversation, which is the part a manager most wants to hear.
    tap = _tap_step(tap_websocket)
    if tap:
        steps.append(tap)
    connect: dict = {
        "to": lead_number,
        "from": caller_id,
        # Default is 60s; shorter so a rep working a list is not left waiting, and short
        # of most voicemail pickups so "no answer" stays "no answer".
        "timeout": 30,
    }
    # Two parameters are deliberately absent, both removed after breaking live calls:
    #
    #   ringback        -- takes an array of play URIs. ["ring"] is not one.
    #   answer_on_bridge -- documented, correctly named, and it still produced the same
    #                       symptom: one ring, then the call cuts off. On a WebRTC leg
    #                       dialling a Resource Address, leaving the A-leg unanswered
    #                       evidently does not survive, whatever the reference says about
    #                       the parameter in isolation.
    #
    # The cost of it being wrong is every call the sales floor makes, and it has now been
    # wrong twice. It stays out until there is a way to try it that is not production.
    #
    # The consequence is the known UI fault: SignalWire answers this leg to run the
    # script, so the softphone reports connected before the callee picks up. That is a
    # display problem. Calls not connecting is not.
    if call_state_webhook:
        # The far end's own progress, which nothing else reports. The resource-level
        # Status Change Webhook describes this script's leg, not the leg being dialled --
        # which is why sw-call-status has never once fired despite being configured.
        connect["call_state_url"] = call_state_webhook
        connect["call_state_events"] = ["created", "ringing", "answered", "ended"]
    steps.append({"connect": connect})
    return {"sections": {"main": steps}}


# How our own name has to be SPELLED so a speech engine SAYS it correctly.
#
# "Sysevo" is not a word, so TTS reads it letter-pattern-wise and lands on
# "Sys-AY-vo". Every caller who reached the hold greeting heard the company
# introduce itself by the wrong name. The doubled s closes the first syllable
# and "ee" forces the long second one, giving SIS-ee-vo.
#
# DO NOT "correct" this spelling. It is wrong on purpose, and it is only ever
# passed to a say: URI - never written to a caller, a record or an email, where
# the real spelling is the only acceptable one.
SPOKEN_COMPANY_NAME = "Sisseevo"


def build_inbound_hold_swml(
    *,
    conference_name: str,
    recording_webhook: str,
    greeting: str = "",
    tap_websocket: str = "",
) -> dict:
    """Answer an inbound caller and hold them in a room while reps ring.

    The conference name is the correlation key an accepting rep joins by, so it
    must be the same string stored on the inbound_calls row.

    join_conference, not join_room.

    join_room DOES NOT WORK for this, and that is measured rather than reasoned.
    SignalWire's own voice log for leg f939e275, an answered internal call:

        01:25:39  answered, SWML returned join_room
        01:27:20  calling_error   {"code": "500", "message": "Internal server error"}
                  request: {"method": "join_room", "swml": true}
        01:27:20  calling_call_state  call_state "ending", end_reason "error"

    101 seconds after answering, the join_room method threw a 500 from
    SignalWire's side -- relay_script_method_execute_failed -- and the leg was
    torn down. audio_in_mos on that leg was 4.49, so the audio was fine right up
    to the moment the verb failed. The conference the other party was in carried
    on for another seventy seconds and kept billing, which is why this presented
    as "the call cut the recipient off after about a minute and a half".

    It is also the wrong verb on its own terms: join_room accepts exactly one
    parameter, `name`, and joins a VIDEO room, so no hold behaviour could be
    expressed on it at all -- start_on_enter and end_on_exit are what decide
    whether a waiting caller gets hold treatment or sits in a live empty room.

    https://signalwire.com/docs/swml/reference/calling/join-conference
    """
    steps: list[dict] = []
    if greeting:
        steps.append({"play": {"url": f"say:{greeting}"}})
    if recording_webhook:
        # Recording starts before the join so the greeting and any hold time are
        # captured - a recording that begins when the rep answers loses the
        # reason the caller rang.
        steps.append(
            {
                "record_call": {
                    "stereo": True,
                    "format": "mp3",
                    "status_url": recording_webhook,
                }
            }
        )
    tap = _tap_step(tap_websocket)
    if tap:
        steps.append(tap)
    steps.append(
        {
            "join_conference": {
                "name": conference_name,
                # The caller is NOT the main participant. Left at its default of true,
                # the conference starts the moment the caller lands in it, so they sit
                # in a live room on their own -- which is the silence this function's
                # old KNOWN GAP note described. False holds them until a rep actually
                # arrives, which is when conference hold treatment applies.
                "start_on_enter": False,
                # The caller hanging up must not tear down a conference a rep may be
                # halfway into joining.
                "end_on_exit": False,
            }
        }
    )
    return {"sections": {"main": steps}}


def build_no_agents_swml(
    *,
    message: str = (
        "Sorry, there is nobody available to take your call right now. "
        "Please try again shortly."
    ),
) -> dict:
    """Turn the caller away honestly when no rep can be rung.

    The alternative -- the normal hold SWML -- greets the caller with
    "connecting you now" and drops them into a conference nobody will ever
    join, so they hear hold music until they give up. That reads to the caller
    as a broken phone system, and it bills for the whole dead call. Saying so
    and hanging up is worse for the caller than being answered and better than
    being lied to.
    """
    return {
        "sections": {
            "main": [
                {"play": {"url": f"say:{message}"}},
                {"hangup": {}},
            ]
        }
    }


def build_conference_join_swml(*, conference_name: str) -> dict:
    """Put an accepting rep into the caller's conference.

    The rep IS the main participant: the conference starts when they arrive and
    ends when they leave. Without end_on_exit the caller is left sitting alone
    in a room after the rep hangs up, with nothing telling them the call is over.

    No recording here: the caller's own leg is already recording, and a second
    recorder would bill twice for one conversation and produce two files that
    have to be reconciled later.
    """
    return {
        "sections": {
            "main": [
                {
                    "join_conference": {
                        "name": conference_name,
                        "start_on_enter": True,
                        "end_on_exit": True,
                    }
                }
            ]
        }
    }
