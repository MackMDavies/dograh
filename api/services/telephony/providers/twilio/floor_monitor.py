"""Request-scoped Floor audit state; only verified manager SDK legs may join."""
import asyncio
import re
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from loguru import logger

from api.constants import SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
from api.services.telephony.providers.twilio.dialer_conference import _twilio_client, cancel_call

_SID = re.compile(r"^CA[0-9a-f]{32}$")
_CONFERENCE = re.compile(r"^CF[0-9a-f]{32}$")
_RPC_FAILED = object()


async def _rpc(name: str, values: dict):
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return _RPC_FAILED
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/{name}", json=values,
                headers={"apikey": SUPABASE_ANON_KEY,
                         "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                         "Content-Type": "application/json"},
            )
            response.raise_for_status()
            return response.json()
    except Exception as exc:  # A state outage must never open live audio.
        logger.warning("Floor session state unavailable: {}", type(exc).__name__)
        return _RPC_FAILED


async def close_floor_monitor_for_provider(manager_call_sid: str) -> bool | None:
    """False means this participant was not a manager; None requests a webhook retry."""
    if not _SID.fullmatch(manager_call_sid):
        return False
    result = await _rpc("dialer_floor_monitor_left", {"p_manager_call_sid": manager_call_sid})
    return result if isinstance(result, bool) else None


async def claim_floor_monitor(
    *, parent_call_sid: str, manager_user_id: str, manager_call_sid: str,
    mode: str, session_id: str | None = None,
) -> dict | None:
    if not _SID.fullmatch(parent_call_sid) or not _SID.fullmatch(manager_call_sid):
        return None
    if parent_call_sid == manager_call_sid or mode not in {"listen", "whisper", "barge"}:
        return None
    legacy = session_id is None
    try:
        request_id = str(UUID(session_id)) if session_id is not None else str(
            uuid5(NAMESPACE_URL, f"sysevo-floor:{manager_call_sid}")
        )
        manager_id = str(UUID(manager_user_id))
    except (TypeError, ValueError, AttributeError):
        return None
    values = {"p_session_id": request_id, "p_parent_call_sid": parent_call_sid,
              "p_manager_user_id": manager_id, "p_manager_call_sid": manager_call_sid,
              "p_mode": mode, "p_legacy": legacy}
    for _ in range(2):
        result = await _rpc("dialer_floor_monitor_join", values)
        if not isinstance(result, dict):
            return None
        if result.get("allowed") is True:
            conference = result.get("conference_name")
            coach = result.get("coach_call_sid")
            if not isinstance(conference, str) or not conference:
                return None
            if mode == "whisper" and (not isinstance(coach, str) or not _SID.fullmatch(coach)):
                return None
            return result
        previous = result.get("previous_manager_call_sid")
        if (not isinstance(previous, str) or not _SID.fullmatch(previous)
                or previous in {parent_call_sid, manager_call_sid}):
            return None
        # SQL returns only a previously verified manager leg, never a prospect or
        # rep. End that leg before a second mode can be recorded or hear audio.
        if not await cancel_call(call_sid=previous):
            return None
        if await close_floor_monitor_for_provider(previous) is not True:
            return None
    return None


async def change_floor_monitor_mode(*, session_id: str, manager_user_id: str, mode: str) -> str:
    """Update the existing manager participant, then persist provider confirmation."""
    if mode not in {"listen", "whisper", "barge"}:
        return "unavailable"
    try:
        values = {"p_session_id": str(UUID(session_id)), "p_manager_user_id": str(UUID(manager_user_id)),
                  "p_change_id": str(uuid4()), "p_mode": mode}
    except (ValueError, TypeError, AttributeError):
        return "unavailable"
    target = await _rpc("dialer_floor_monitor_mode_prepare", values)
    if target is _RPC_FAILED:
        return "failed"
    if not isinstance(target, dict) or target.get("allowed") is not True:
        return "busy" if isinstance(target, dict) and target.get("reason") == "busy" else "unavailable"
    manager_sid = target.get("manager_call_sid")
    coach_sid = target.get("coach_call_sid")
    if (not isinstance(manager_sid, str) or not _SID.fullmatch(manager_sid)
            or manager_sid in (coach_sid, target.get("prospect_call_sid"))
            or (mode == "whisper" and (not isinstance(coach_sid, str) or not _SID.fullmatch(coach_sid)))):
        await _rpc("dialer_floor_monitor_mode_release", {key: value for key, value in values.items() if key != "p_mode"})
        return "failed"
    try:
        client = await _twilio_client()
        if client is None:
            raise RuntimeError("Independent dialer credentials unavailable")
        conference_sid = target.get("conference_sid")
        if not isinstance(conference_sid, str) or not _CONFERENCE.fullmatch(conference_sid):
            conferences = await asyncio.to_thread(client.conferences.list,
                friendly_name=target["conference_name"], status="in-progress", limit=2)
            if len(conferences) != 1:
                raise RuntimeError("One active conference is required")
            conference_sid = conferences[0].sid
        if not isinstance(conference_sid, str) or not _CONFERENCE.fullmatch(conference_sid):
            raise RuntimeError("Invalid conference identity")
        changes: dict[str, bool | str] = {"muted": mode == "listen", "coaching": mode == "whisper"}
        if mode == "whisper":
            changes["call_sid_to_coach"] = coach_sid
        participant = await asyncio.to_thread(
            client.conferences(conference_sid).participants(manager_sid).update, **changes)
        if (participant.muted is not (mode == "listen")
                or participant.coaching is not (mode == "whisper")
                or (mode == "whisper" and participant.call_sid_to_coach != coach_sid)):
            raise RuntimeError("Provider did not confirm the requested mode")
        if await _rpc("dialer_floor_monitor_mode_commit", values) is not True:
            raise RuntimeError("Mode change could not be committed")
        return "changed"
    except Exception as exc:
        logger.warning("Floor mode change not confirmed: {}", type(exc).__name__)
        # End only this verified manager leg on uncertain mode/audit state. The
        # rep and prospect are never cancellation targets on this path.
        await cancel_call(call_sid=manager_sid)
        await _rpc("dialer_floor_monitor_mode_release", {key: value for key, value in values.items() if key != "p_mode"})
        return "failed"
