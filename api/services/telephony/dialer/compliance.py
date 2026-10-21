"""Server-side compliance checks shared by dialer telephony providers.

Policy (2026-10-21, owner decision): calling hours are ENFORCED as a two-step warning.
The browser asks the server's own check before every outbound dial; outside hours the
rep must click "Call now" and then "Are you sure?" (state fines), and only that confirmed
dial (HoursOverride=1) passes the server's hours gate. The do-not-call register is never
overridable. Parallel batches have no per-call confirmation, so they dial only leads the
server confirms are inside hours and skip the rest (never refusing the whole batch).
"""

import asyncio

import httpx
from loguru import logger

from api.constants import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL


def _headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


async def phone_block_reason(phone: str) -> str | None:
    """Return a suppression/terminal reason, or fail closed on uncertainty (after one retry)."""
    reason = await _phone_block_reason_once(phone)
    if reason == "compliance_unavailable":
        await asyncio.sleep(0.3)
        reason = await _phone_block_reason_once(phone)
    return reason


async def _phone_block_reason_once(phone: str) -> str | None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return "compliance_unavailable"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/dialer_service_phone_block_reason",
                json={"p_phone": phone},
                headers=_headers(),
            )
            response.raise_for_status()
            reason = response.json()
        return reason if isinstance(reason, str) and reason else None
    except Exception as exc:  # noqa: BLE001 - unknown compliance state blocks the dial
        logger.error(f"Could not verify dialer phone suppression: {exc}")
        return "compliance_unavailable"


# The lead row could not vouch for this number (the rep dialed another number of the
# lead, or the entry is gone). Hours are then judged from the number alone.
_ENTRY_REASONS = frozenset({
    "lead_unavailable", "lead_phone_mismatch", "account_unavailable", "account_mismatch",
})


async def calling_hours_detail(phone: str, entry_id: str | None = None) -> dict:
    """The hours decision with its context: {allowed, reason, timezone, us_state, retry_at}.

    One retry on an unreachable service; a lead row that cannot vouch for the number
    falls back to the number alone. Never treats uncertainty as permission.
    """
    result = await _calling_hours_raw(phone, entry_id)
    if result.get("reason") == "compliance_unavailable":
        await asyncio.sleep(0.3)
        result = await _calling_hours_raw(phone, entry_id)
    if entry_id and result.get("reason") in _ENTRY_REASONS:
        result = await _calling_hours_raw(phone, None)
    return result


async def calling_hours_result(
    phone: str, entry_id: str | None = None
) -> tuple[bool, str | None, str | None]:
    """Return the server compliance decision and its authoritative US state."""
    result = await calling_hours_detail(phone, entry_id)
    state = result.get("us_state")
    return bool(result.get("allowed")), result.get("reason"), state if isinstance(state, str) else None


async def _calling_hours_raw(phone: str, entry_id: str | None) -> dict:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return {"allowed": False, "reason": "compliance_unavailable"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{SUPABASE_URL}/functions/v1/dialer-calling-hours-check",
                json={"phone": phone, "entry_id": entry_id},
                headers=_headers(),
            )
            response.raise_for_status()
            result = response.json()
    except Exception as exc:  # noqa: BLE001 - never treat uncertainty as permission
        logger.error(f"Could not verify dialer calling hours: {exc}")
        return {"allowed": False, "reason": "compliance_unavailable"}
    if not isinstance(result, dict):
        return {"allowed": False, "reason": "compliance_unavailable"}
    allowed = result.get("allowed") is True
    reason = result.get("reason")
    return {
        "allowed": allowed,
        "reason": None if allowed else (reason if isinstance(reason, str) else "compliance_unavailable"),
        "timezone": result.get("timezone") if isinstance(result.get("timezone"), str) else None,
        "us_state": result.get("us_state") if isinstance(result.get("us_state"), str) else None,
        "retry_at": result.get("retry_at") if isinstance(result.get("retry_at"), str) else None,
    }


async def calling_hours_allowed(phone: str, entry_id: str | None = None) -> tuple[bool, str | None]:
    """Compatibility wrapper for callers that need only allow/block."""
    allowed, reason, _state = await calling_hours_result(phone, entry_id)
    return allowed, reason


# The rep saw the two-step calling-hours warning and confirmed it. Only the hours
# reasons can be overridden (including "the hours could not be confirmed", which the
# warning also shows); the do-not-call register never.
HOURS_OVERRIDE_REASONS = frozenset({
    "outside_calling_hours", "public_holiday", "timezone_unavailable", "compliance_unavailable",
})


def rep_overrides_hours(reason: str | None, override_flag: object) -> bool:
    """True when a refused dial may go ahead because the rep confirmed it."""
    confirmed = str(override_flag or "").strip().lower() in {"1", "true", "yes"}
    return confirmed and str(reason or "") in HOURS_OVERRIDE_REASONS


# What the rep hears when the server refuses a single dial.
_DIAL_BLOCK_MESSAGES = {
    "do_not_call": "This number is on the do not call list. The call was not placed.",
    "outside_calling_hours": "This lead is outside calling hours right now. The call was not placed.",
    "public_holiday": "It is a public holiday where this lead is. The call was not placed.",
    "timezone_unavailable": "This lead's time zone could not be confirmed, so the call was not placed.",
    "compliance_unavailable": "The calling hours check is unavailable right now. Please try again in a minute.",
}


def dial_block_message(reason: str | None) -> str:
    return _DIAL_BLOCK_MESSAGES.get(
        str(reason or ""), "This call could not be placed. Please check the lead and try again."
    )



PARALLEL_LINE_LADDER = (2, 3, 5, 10)


def snap_parallel_lines(count: int) -> int:
    """Largest supported batch size <= count (0 when fewer than two leads remain)."""
    return max((size for size in PARALLEL_LINE_LADDER if size <= count), default=0)


async def callable_batch_entries(
    items: list[tuple[str, str]],
) -> tuple[list[str], dict[str, str]]:
    """Split (entry_id, phone) pairs into the ids to dial (in order) and the skipped ones.

    A batch has no per-call confirmation, so a lead is skipped unless the server can
    confirm it is inside its calling hours and not on the do-not-call register; every
    confirmed lead dials. Skipped leads stay in the queue for a single call, where the
    rep sees the warning. Lookups run concurrently.
    """
    blocks, hours = await asyncio.gather(
        asyncio.gather(*(phone_block_reason(phone) for _entry, phone in items)),
        asyncio.gather(*(calling_hours_allowed(phone, entry) for entry, phone in items)),
    )
    keep: list[str] = []
    skipped: dict[str, str] = {}
    for (entry_id, _phone), block, (allowed, reason) in zip(items, blocks, hours):
        if block:
            skipped[entry_id] = block
        elif not allowed:
            skipped[entry_id] = str(reason or "compliance_unavailable")
        else:
            keep.append(entry_id)
    return keep, skipped


BATCH_TOO_SMALL_MESSAGE = (
    "Fewer than two leads in this batch can be called right now. "
    "Switch to single calls for the rest of this list."
)
