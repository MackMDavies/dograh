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
        # Long enough for voicemail to pick up. It was 30s, chosen to be "short of most
        # voicemail pickups so no answer stays no answer" -- which is the opposite of
        # what reps need: a mailbox is somewhere to leave a message (and the dialer now
        # offers to send their recorded one), and carriers commonly divert to it at
        # 20-40s, so a 30s cap hung up on a share of them seconds before the greeting.
        # Short of SignalWire's 60s default so a genuinely unanswered line still frees
        # the rep for the next dial.
        #
        # RING timeout only. It bounds how long we wait for an answer and has no
        # bearing on how long the conversation may run -- worth stating, because it
        # was the first thing suspected when reps reported being cut off mid-call.
        "timeout": 55,
        # How long the CONVERSATION may run, stated rather than inherited.
        #
        # A rep on a good call can be on it for half an hour, and the requirement is
        # that nothing shortens that. Leaving this unset left the limit to whatever
        # SignalWire's default happens to be -- a number nobody here has verified and
        # which could change without us noticing. Four hours is far beyond any real
        # sales call, so this can only ever be the thing that does NOT end a call,
        # while making the intent greppable for the next person who goes looking for
        # a cap.
        "max_duration": 14400,
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


def build_agent_overflow_swml(
    *,
    agent_number: str,
    caller_id: str | None = None,
    recording_webhook: str = "",
    tap_websocket: str = "",
    fallback_message: str = (
        "Sorry, there is nobody available to take your call right now. "
        "Please try again shortly."
    ),
) -> dict:
    """Hand a caller nobody can answer to the inbound AI agent (Sam INBOUND Sales).

    On 2026-09-25 a prospect rang a rep's number back three times before getting an
    answer: twice the rep was already on a call, so there was nobody to ring and
    build_no_agents_swml told her so and hung up. Four of six callbacks to that rep's
    number in four days went unanswered. A prospect calling back is the warmest lead
    the floor has; losing them to "nobody available" is the worst outcome.

    The agent lives on Dograh, which carries its calls on Twilio -- there is no
    SignalWire provider there, deliberately -- so the caller is bridged to the agent's
    own phone number with connect, exactly like a rep's outbound leg.

    `from` is left unset by default, so SignalWire presents the CALLER's own number
    (connect.from defaults to the calling party). That is what the agent needs: its
    caller memory and CRM lookups are keyed on the number it sees, and with our rep's
    number there it greeted every returning prospect as a stranger. Pass caller_id only
    to override it.

    If the agent cannot be reached, the caller still gets the honest message rather
    than silence: connect_result is anything but "connected".
    """
    steps: list[dict] = []
    if recording_webhook:
        steps.append(
            {"record_call": {"stereo": True, "format": "mp3", "status_url": recording_webhook}}
        )
    tap = _tap_step(tap_websocket)
    if tap:
        steps.append(tap)
    connect: dict = {"to": agent_number, "timeout": 30, "max_duration": 7200}
    if caller_id:
        connect["from"] = caller_id
    steps.append({"connect": connect})
    steps.append(
        {
            "switch": {
                "variable": "connect_result",
                "case": {"connected": [{"hangup": {}}]},
                "default": [
                    {"play": {"url": f"say:{fallback_message}"}},
                    {"hangup": {}},
                ],
            }
        }
    )
    return {"sections": {"main": steps}}

