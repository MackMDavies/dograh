"""The voice agent's "book a callback" tool for dialer extra answers (Phase 1b gap).

When nobody on the team frees up, the agent agrees a time with the person. This tool
stores THAT time: ``dialer_agent_book_callback`` sets the lead's callback_scheduled_at
(callback_later, callback_priority = true, a note) and records the 'callback_booked'
stage; the chain's own callback at the end of the call then keeps the agreed time
instead of "now".

Only registered on dialer agent runs (``dialer_extra_answer_id`` in the run context).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

from api.constants import SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL

TOOL_NAME = "book_callback"
MIN_AHEAD = timedelta(minutes=5)
MAX_AHEAD = timedelta(days=30)


def tool_description(now: datetime, tz_name: str | None) -> str:
    local = now.astimezone(_zone(tz_name))
    return (
        "Book the callback time you agreed with the person, once they have confirmed it. "
        "Repeat the time back to them first. Pass `when` as an ISO 8601 date-time WITH a "
        f"UTC offset. It is now {local.strftime('%A %d %B %Y %H:%M')} "
        f"({local.strftime('%z')}) where the person is. Only use this for a time they agreed."
    )


def tool_schema_properties() -> dict[str, Any]:
    return {
        "when": {"type": "string", "description": "Agreed callback time, ISO 8601 with offset, e.g. 2026-10-21T15:30:00-05:00"},
        "note": {"type": "string", "description": "Anything the rep should know (max one sentence)."},
    }


def _zone(name: str | None):
    try:
        return ZoneInfo(name) if name else UTC
    except Exception:  # noqa: BLE001 - unknown zone: UTC
        return UTC


def parse_when(value: Any, *, now: datetime, tz_name: str | None = None) -> datetime | None:
    """An aware datetime from the agent's ISO string; a naive one is read in the
    person's timezone. None when it cannot be read or is in the past."""
    text = str(value or "").strip()
    if not text or len(text) > 40:
        return None
    text = re.sub(r"Z$", "+00:00", text)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_zone(tz_name))
    if moment < now - timedelta(minutes=1):
        return None
    return moment


def clamp(moment: datetime, *, now: datetime) -> datetime:
    return min(max(moment, now + MIN_AHEAD), now + MAX_AHEAD)


async def book(prospect_call_id: str, due_at: datetime, note: str = "") -> dict[str, Any] | None:
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY) or not prospect_call_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/dialer_agent_book_callback",
                json={"p_prospect_call_id": prospect_call_id, "p_due_at": due_at.isoformat(),
                      "p_note": " ".join(str(note or "").split())[:300]},
                headers={"apikey": SUPABASE_ANON_KEY or SUPABASE_SERVICE_ROLE_KEY,
                         "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                         "Content-Type": "application/json"},
            )
            response.raise_for_status()
            result = response.json()
    except Exception as exc:  # noqa: BLE001 - the chain still queues a callback at the end
        logger.warning(f"Agent callback booking failed: {type(exc).__name__}")
        return None
    return result if isinstance(result, dict) else None


async def handle_tool_call(arguments: dict[str, Any], context: dict[str, Any], *,
                           now: datetime | None = None, booker=book) -> dict[str, Any]:
    """The tool's result for the LLM."""
    now = now or datetime.now(UTC)
    call_id = str(context.get("dialer_original_call_id") or "")
    if not context.get("dialer_extra_answer_id") or not call_id:
        return {"status": "error", "reason": "callbacks can't be booked on this call"}
    moment = parse_when(arguments.get("when"), now=now, tz_name=context.get("phone_timezone"))
    if moment is None:
        return {"status": "error", "reason": "give the agreed time as an ISO 8601 date-time in the future"}
    due = clamp(moment, now=now)
    stored = await booker(call_id, due, str(arguments.get("note") or ""))
    if not stored:
        return {"status": "error", "reason": "the callback could not be saved; tell them the rep will call back soon"}
    return {"status": "booked", "due_at": str(stored.get("due_at") or due.isoformat()),
            "note": "Booked. Confirm the time once more, thank them and close politely."}


__all__ = ["TOOL_NAME", "book", "clamp", "handle_tool_call", "parse_when", "tool_description", "tool_schema_properties"]
