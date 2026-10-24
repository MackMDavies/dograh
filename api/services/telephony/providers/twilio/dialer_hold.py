"""Carrier-driven parallel Hold flow; the same answered call is retained."""
from urllib.parse import urlencode
from xml.sax.saxutils import escape

from api.services.telephony.dialer import extra_answer
from api.services.telephony.dialer.inbound_call_log import notify_inbound_call
from api.services.telephony.providers.twilio import dialer_agent_handoff
from api.services.telephony.providers.twilio.dialer_twiml import conference_telemetry_attributes
from api.utils.common import get_backend_endpoints

HANGUP = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'


async def hold_twiml(*, parent_call_sid: str, child_call_sid: str, leg_id: str, account_sid: str, begin: bool = False) -> str | None:
    state = await dialer_agent_handoff._rpc("dialer_resolve_twilio_parallel_hold",{
        "p_parent_call_sid":parent_call_sid,"p_child_call_sid":child_call_sid,"p_leg_id":leg_id,
        "p_account_sid":account_sid,"p_begin":begin,
    })
    if not isinstance(state,dict):
        return None
    if state.get("status") == "completed":
        return HANGUP
    if state.get("status") == "agent":
        try:
            prepared = await dialer_agent_handoff.prepare_agent_handoff(
                parent_call_sid=parent_call_sid,child_call_sid=child_call_sid,leg_id=leg_id,
                account_sid=account_sid,allow_fallback=True,
            )
        except dialer_agent_handoff.AgentHandoffUnavailable as exc:
            if exc.retryable:
                raise
            return HANGUP
        return prepared.twiml if prepared else HANGUP
    backend,_ = await get_backend_endpoints()
    if not str(backend).startswith("https://"):
        raise dialer_agent_handoff.AgentHandoffUnavailable("Secure Hold callback endpoint is unavailable")
    endpoint = str(backend).rstrip("/")+"/api/v1/telephony"
    if state.get("status") == "offered":
        # SQL already persisted the original-rep invitation and its durable
        # deadline together. Never create a PSTN call to the rep or prospect.
        expected_conference = "call-"+child_call_sid
        if state.get("conference_name") != expected_conference:
            raise dialer_agent_handoff.AgentHandoffUnavailable("Hold conference identity mismatch")
        attrs = conference_telemetry_attributes(
            events_url=endpoint+"/dialer-conference-events",recording_url=endpoint+"/dialer-recording-callback",
        )
        if state.get("inbound_call_id"):
            notify_inbound_call(str(state["inbound_call_id"]))
        return ('<?xml version="1.0" encoding="UTF-8"?><Response><Dial><Conference '+attrs+
            'startConferenceOnEnter="false" endConferenceOnExit="true" beep="false">'+
            escape(expected_conference)+'</Conference></Dial></Response>')
    if state.get("status") != "waiting":
        raise dialer_agent_handoff.AgentHandoffUnavailable("Unknown Hold state",retryable=True)
    redirect = endpoint+"/dialer-parallel-hold-next?"+urlencode({"parent_call_sid":parent_call_sid,"leg_id":leg_id})
    notice = ('<Say>'+extra_answer.RECORDING_DISCLOSURE+'</Say>') if extra_answer.AUTO_RECORDING_NOTICE else ''
    intro = (notice+'<Say>Thanks for picking up, connecting you now.</Say>') if begin else ''
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'+intro+
        '<Pause length="1"/><Redirect method="POST">'+escape(redirect)+'</Redirect></Response>')


async def independent_children(parent_call_sid: str) -> set[str]:
    calls = await dialer_agent_handoff._rpc("dialer_parallel_independent_children",{"p_parent_call_sid":parent_call_sid})
    if not isinstance(calls,list) or any(not isinstance(call,str) for call in calls):
        raise dialer_agent_handoff.AgentHandoffUnavailable("Independent-call ownership is unavailable",retryable=True)
    return set(calls)


async def record_ended(*, child_call_sid: str, ended_at: str) -> None:
    await dialer_agent_handoff._rpc("dialer_end_twilio_parallel_hold",{
        "p_child_call_sid":child_call_sid,"p_ended_at":ended_at,
    })
