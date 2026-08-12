"""Twilio Conference helpers for live call monitoring.

Every dialer call is a named Conference ("call-{parent_call_sid}") instead
of a plain <Dial><Number> bridge, specifically so a manager can join
mid-call as a muted listener - Twilio has no way to inject a third
participant into an already-bridged two-party <Dial> call. See
docs/superpowers/specs/2026-08-12-dialer-live-call-monitoring-design.md
in the sysevo repo.
"""

import asyncio
import os

from loguru import logger
from twilio.rest import Client


def conference_name_for(parent_call_sid: str) -> str:
    return f"call-{parent_call_sid}"


def parent_call_sid_from_conference_name(name: str) -> str | None:
    if not name.startswith("call-"):
        return None
    return name.removeprefix("call-")


def _twilio_client() -> Client | None:
    account_sid = os.environ.get("SYSEVO_TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("SYSEVO_TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        return None
    return Client(account_sid, auth_token)


async def dial_lead_into_conference(
    *,
    parent_call_sid: str,
    lead_number: str,
    caller_id: str,
    join_conference_url: str,
    status_callback_url: str,
) -> str | None:
    """Places the outbound leg to the lead, joining them into the same
    conference as the rep, with status tracking attached the same way the
    old <Number statusCallback> used to work - just via the REST API's own
    status_callback params instead of embedded TwiML, since there's no
    <Number> noun anymore. Returns the new call's SID, or None on failure -
    fails soft, must never break the rep's already-connected call.

    Twilio's Python SDK is synchronous - client.calls.create() is a
    blocking HTTP call, so it's wrapped in asyncio.to_thread() to avoid
    blocking the FastAPI event loop (and every other concurrent request)
    for the time it takes Twilio to respond.
    """
    client = _twilio_client()
    if not client:
        logger.error("dial_lead_into_conference: Twilio credentials not configured")
        return None
    try:
        call = await asyncio.to_thread(
            client.calls.create,
            to=lead_number,
            from_=caller_id,
            url=join_conference_url,
            method="POST",
            status_callback=status_callback_url,
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
        )
        return call.sid
    except Exception as exc:  # noqa: BLE001 - deliberate: must never raise into voice-connect
        logger.error(f"Failed to dial lead into conference for {parent_call_sid}: {exc}")
        return None
