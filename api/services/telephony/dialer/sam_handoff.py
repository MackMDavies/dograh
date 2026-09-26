"""Moving a live inbound caller between the reps' dialer and Sam INBOUND Sales.

The rules live in sam_handoff_rules.py; this is the I/O. Like the rest of the dialer's
inbound path, nothing here raises into a live call: every failure is logged and leaves
the call where it was, which is the behaviour before these hand-offs existed.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from loguru import logger

from api.constants import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
from api.services.telephony.dialer.sam_handoff_rules import (
    LIVE_CALL_WINDOW_MINUTES,
    REP_ANSWER_SECONDS,
    calling_transfer_body,
    phone_tail,
)
from api.services.telephony.dialer.signalwire_dialer import _space_host

_INBOUND = "/rest/v1/inbound_calls"
_background: set[asyncio.Task] = set()


def spawn(coro) -> None:
    """Run a coroutine after the response has gone out, and keep a reference to it."""
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        coro.close()
        return
    _background.add(task)
    task.add_done_callback(_background.discard)


def _headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def move_call(call_id: str, swml_url: str) -> bool:
    """Point a live call at new SWML (SignalWire Calling API calling.transfer)."""
    host = _space_host()
    project = (os.environ.get("SIGNALWIRE_PROJECT_ID") or "").strip()
    token = (os.environ.get("SIGNALWIRE_API_TOKEN") or "").strip()
    if not (host and project and token and swml_url):
        logger.error("sam-handoff: cannot move call - SignalWire credentials or URL missing")
        return False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://{host}/api/calling/calls",
                json=calling_transfer_body(call_id, swml_url),
                auth=(project, token),
                timeout=8.0,
            )
        # Said out loud either way: calling.transfer on these calls was established from
        # SignalWire's docs, and the first real hand-off has to confirm it.
        logger.info(f"sam-handoff: calling.transfer {call_id} -> {response.status_code} {response.text[:200]}")
        return response.is_success
    except Exception as exc:  # noqa: BLE001 - a live caller is on this call
        logger.error(f"sam-handoff: calling.transfer {call_id} failed: {exc}")
        return False


async def _patch(filters: dict[str, str], patch: dict[str, Any]) -> list[dict[str, Any]]:
    """A conditional update. Returns the rows it changed — empty means the condition no
    longer held (a rep answered, the caller hung up), which is how races are avoided."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return []
    try:
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{SUPABASE_URL}{_INBOUND}", params=filters, json=patch, headers=_headers(), timeout=4.0
            )
            response.raise_for_status()
            rows = response.json()
            return rows if isinstance(rows, list) else []
    except Exception as exc:  # noqa: BLE001
        logger.error(f"sam-handoff: inbound_calls update failed: {exc}")
        return []


async def mark_missed_if_ringing(call_id: str) -> bool:
    """Stop ringing the reps — only if nobody has answered. True when this call is ours to move."""
    rows = await _patch(
        {"provider_call_id": f"eq.{call_id}", "status": "eq.ringing", "ended_at": "is.null"},
        {"status": "missed", "updated_at": "now()"},
    )
    return bool(rows)


async def ring_again(call_id: str, targets: list[str]) -> dict[str, Any] | None:
    """Put a call Sam is holding back in front of these reps' dialers."""
    rows = await _patch(
        {"provider_call_id": f"eq.{call_id}", "status": "eq.missed", "ended_at": "is.null"},
        {
            "status": "ringing",
            "target_user_ids": targets,
            "answered_by": None,
            "answered_at": None,
            "updated_at": "now()",
        },
    )
    return rows[0] if rows else None


async def find_live_call_for(caller_number: str) -> dict[str, Any] | None:
    """The caller's inbound call Sam is holding: recent, handed off, not ended."""
    tail = phone_tail(caller_number)
    if len(tail) < 10 or not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    since = (datetime.now(UTC) - timedelta(minutes=LIVE_CALL_WINDOW_MINUTES)).isoformat()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SUPABASE_URL}{_INBOUND}",
                params={
                    "select": "id,provider_call_id,to_number,from_number,status",
                    "from_number": f"like.*{tail}",
                    "status": "eq.missed",
                    "ended_at": "is.null",
                    "created_at": f"gte.{since}",
                    "order": "created_at.desc",
                    "limit": "1",
                },
                headers=_headers(),
                timeout=4.0,
            )
            response.raise_for_status()
            rows = response.json()
            return rows[0] if rows else None
    except Exception as exc:  # noqa: BLE001
        logger.error(f"sam-handoff: live call lookup failed: {exc}")
        return None


async def hand_to_sam_if_unanswered(call_id: str, after_seconds: int, agent_url: str) -> None:
    """RING FIRST: after the rep's dialer has rung, move an unanswered caller to Sam."""
    await asyncio.sleep(after_seconds)
    if not await mark_missed_if_ringing(call_id):
        return  # answered, or the caller has gone
    if not await move_call(call_id, agent_url):
        logger.error(f"sam-handoff: {call_id} unanswered after {after_seconds}s and could not be moved to Sam")


async def back_to_sam_if_rep_misses(call_id: str, agent_back_url: str) -> None:
    """TRANSFER: the rep did not pick up in time — the caller goes back to Sam."""
    await asyncio.sleep(REP_ANSWER_SECONDS)
    if not await mark_missed_if_ringing(call_id):
        return
    if not await move_call(call_id, agent_back_url):
        logger.error(f"sam-handoff: {call_id} rep missed the transfer and the caller could not be returned to Sam")
