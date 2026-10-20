"""Phase 1b: an extra person is on the line and their rep is not free.

Hold is gone. The person never hears silence, music or "please hold": the dialer walks
the account's chain (``dialer_account_voicemail_settings.extra_answer_chain``, default
ringback → teammate → agent → callback → apology) and moves on the moment a step is not
possible. Postgres owns every decision (migration 20270425100100,
``dialer_extra_answer_*``); this module is the provider-neutral plumbing:

* ``begin`` records the extra answer (a second human on a parallel batch, or a person
  whose ring-back to the original rep was missed) and snapshots the chain;
* ``advance`` reserves the next possible step and returns what to do:
  ``teammate`` (an auto-connect offer to the longest-idle eligible teammate),
  ``agent`` (the outbound voice agent), ``apology`` (play the rep's apology clip, or
  neutral speech, then hang up) or ``hangup`` (the apology was switched off);
* ``mark`` records what happened (a rep joined, the agent started / handed over, the
  person hung up);
* ``reserve_transfer`` / ``claim_transfer`` run the agent's 90 s transfer-back window;
* ``claim_work`` is the restart-safe work list for the reconciliation worker.

Contract: nothing here raises into a webhook. A failed read returns None and the
provider code falls back to the apology, never to a silent or parked call.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from loguru import logger

from api.constants import SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL

STEPS = ("ringback", "teammate", "agent", "callback", "apology")
ACTIONS = frozenset({"teammate", "agent", "apology", "hangup", "none"})

# A teammate's softphone answers by itself; a join this slow means it did not.
OFFER_SECONDS = 8
# The agent hands the person back to whoever frees up first within this window.
AGENT_TRANSFER_WINDOW_SECONDS = 90

# Spoken only when the rep has no clip of their own. Short, warm, never "please hold".
NEUTRAL_HELLO = "Hi, one moment."
NEUTRAL_APOLOGY = (
    "Sorry, I've just been pulled onto another call. I'll ring you straight back. Bye for now."
)
RECORDING_DISCLOSURE = "This call may be recorded and monitored for quality assurance."

_CLIP_KINDS = frozenset({"hello", "apology"})
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_ANON_KEY or SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


def _ready() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


async def _rpc(name: str, payload: dict[str, Any], *, timeout: float = 5.0) -> Any:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/{name}", json=payload, headers=_headers(),
        )
        response.raise_for_status()
        return response.json()


async def _rpc_dict(name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if not _ready():
        return None
    try:
        result = await _rpc(name, payload)
    except Exception as exc:  # noqa: BLE001 - callers fall back to the apology
        logger.warning(f"Extra-answer {name} unavailable: {type(exc).__name__}")
        return None
    return result if isinstance(result, dict) else None


def first_name(name: Any) -> str:
    text = str(name or "").strip()
    return text.split()[0] if text else ""


async def begin(
    *, provider: str, prospect_call_id: str, source: str, parent_call_sid: str,
    leg_id: str | None = None, session_id: str | None = None, account_sid: str | None = None,
) -> dict[str, Any] | None:
    """Record the extra answer (idempotent per provider call). None: not ours / unavailable."""
    if provider not in {"twilio", "signalwire"} or source not in {"parallel", "ringback"}:
        return None
    return await _rpc_dict("dialer_extra_answer_begin", {
        "p_provider": provider, "p_prospect_call_id": prospect_call_id, "p_source": source,
        "p_parent_call_sid": parent_call_sid, "p_leg_id": leg_id, "p_session_id": session_id,
        "p_account_sid": account_sid,
    })


async def advance(
    *, provider: str, prospect_call_id: str, after: str | None = None,
    conference_name: str | None = None,
) -> dict[str, Any] | None:
    """Reserve the next possible step. ``after`` is the step that just failed or ran out.

    Returns the row state plus ``action``. A concurrent caller that already moved the
    chain on gets ``action == 'none'``, so a timer and the worker never skip a step.
    """
    if after is not None and after not in STEPS:
        return None
    state = await _rpc_dict("dialer_extra_answer_advance", {
        "p_provider": provider, "p_prospect_call_id": prospect_call_id,
        "p_after": after, "p_conference_name": conference_name,
    })
    if state is None or state.get("action") not in ACTIONS:
        return None
    return state


async def mark(
    provider: str, prospect_call_id: str, event: str, detail: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if event not in {"rep_joined", "agent_started", "agent_transferred", "callback_requested", "ended"}:
        return None
    return await _rpc_dict("dialer_extra_answer_mark", {
        "p_provider": provider, "p_prospect_call_id": prospect_call_id,
        "p_event": event, "p_detail": detail or {},
    })


async def reserve_transfer(prospect_call_id: str) -> dict[str, Any] | None:
    return await _rpc_dict("dialer_extra_answer_reserve_transfer", {"p_prospect_call_id": prospect_call_id})


async def claim_transfer(prospect_call_id: str) -> dict[str, Any] | None:
    return await _rpc_dict("dialer_extra_answer_claim_transfer", {"p_prospect_call_id": prospect_call_id})


async def release_transfer(prospect_call_id: str) -> None:
    if not _ready():
        return
    try:
        await _rpc("dialer_extra_answer_release_transfer", {"p_prospect_call_id": prospect_call_id})
    except Exception as exc:  # noqa: BLE001 - the worker retries the handover
        logger.info(f"Could not release an extra-answer transfer: {type(exc).__name__}")


async def set_summary(prospect_call_id: str, summary: str) -> bool:
    if not summary.strip() or not _ready():
        return False
    try:
        return await _rpc("dialer_extra_answer_set_summary", {
            "p_prospect_call_id": prospect_call_id, "p_summary": summary[:900],
        }) is True
    except Exception as exc:  # noqa: BLE001 - the card is a convenience
        logger.info(f"Could not store the agent summary: {type(exc).__name__}")
        return False


async def prepare_agent_plan(prospect_call_id: str) -> dict[str, Any] | None:
    return await _rpc_dict("dialer_prepare_extra_answer_agent", {"p_prospect_call_id": prospect_call_id})


async def claim_work(limit: int = 20) -> list[dict[str, Any]]:
    if not _ready():
        return []
    try:
        rows = await _rpc("dialer_extra_answer_claim_work", {"p_limit": limit})
    except Exception as exc:  # noqa: BLE001 - the next tick retries
        logger.warning(f"Extra-answer work list unavailable: {type(exc).__name__}")
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


async def release_lease(extra_answer_id: str) -> None:
    if not _ready() or not _UUID.fullmatch(str(extra_answer_id or "")):
        return
    try:
        await _rpc("dialer_extra_answer_release_lease", {"p_id": extra_answer_id})
    except Exception as exc:  # noqa: BLE001 - the lease lapses on its own
        logger.info(f"Could not release an extra-answer lease: {type(exc).__name__}")


async def clip_url(user_id: str | None, kind: str) -> str | None:
    """A short-lived URL for the rep's hello / apology clip, or None (use neutral speech)."""
    if kind not in _CLIP_KINDS or not user_id or not _UUID.fullmatch(str(user_id)):
        return None
    clip = await _rpc_dict("dialer_extra_answer_clip", {"p_user_id": user_id, "p_kind": kind})
    if not clip:
        return None
    path = clip.get("storage_path")
    owner = str(clip.get("owner_user_id") or "")
    if not isinstance(path, str) or not re.fullmatch(rf"{re.escape(owner)}/[^/]+", path) or owner != user_id:
        return None
    from api.services.telephony.providers.twilio.dialer_call_log import sign_voicemail_storage_path
    try:
        url = await sign_voicemail_storage_path(path, expires_in=600)
    except Exception as exc:  # noqa: BLE001 - neutral speech instead
        logger.info(f"Could not sign the rep's {kind} clip: {type(exc).__name__}")
        return None
    return url if isinstance(url, str) and url.startswith("https://") else None


async def offer_owner(*, provider: str, prospect_call_id: str) -> str | None:
    """The original rep behind the auto-connect offer for this call (hello clip owner)."""
    if not _ready():
        return None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/inbound_calls",
                params={
                    "select": "extra_answer",
                    "provider_call_id": f"eq.{prospect_call_id}",
                    "provider": f"eq.{provider}",
                    "limit": "1",
                },
                headers=_headers(),
            )
            response.raise_for_status()
            rows = response.json()
    except Exception as exc:  # noqa: BLE001 - neutral hello instead
        logger.info(f"Extra-answer offer owner unavailable: {type(exc).__name__}")
        return None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    payload = rows[0].get("extra_answer")
    owner = payload.get("from_rep_user_id") if isinstance(payload, dict) else None
    return str(owner) if isinstance(owner, str) and _UUID.fullmatch(owner) else None


async def prospect_for_conference(*, provider: str, conference_name: str) -> str | None:
    """The person's call id behind an offer's conference."""
    if provider == "twilio":
        match = re.fullmatch(r"call-(CA[0-9a-f]{32})", conference_name or "")
        return match.group(1) if match else None
    if not _ready() or not conference_name:
        return None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/inbound_calls",
                params={
                    "select": "provider_call_id",
                    "conference_name": f"eq.{conference_name}",
                    "provider": "eq.signalwire",
                    "limit": "1",
                },
                headers=_headers(),
            )
            response.raise_for_status()
            rows = response.json()
    except Exception as exc:  # noqa: BLE001 - evidence only
        logger.info(f"Extra-answer conference lookup failed: {type(exc).__name__}")
        return None
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        value = rows[0].get("provider_call_id")
        return str(value) if isinstance(value, str) and value else None
    return None


async def record_rep_joined(
    *, provider: str, conference_name: str, rep_user_id: str,
) -> dict[str, Any] | None:
    """A softphone leg joined an offer's conference. Returns the row (``transfer`` true
    when it was the agent's transfer-back target, whose handover must now start)."""
    prospect = await prospect_for_conference(provider=provider, conference_name=conference_name)
    if not prospect or not rep_user_id:
        return None
    return await mark(provider, prospect, "rep_joined", {"user_id": rep_user_id})


def callback_whisper(rep_name: str | None) -> str:
    who = first_name(rep_name) or "the rep who called them"
    return (
        "Nobody on the team is free to take this call yet. Do not promise a transfer. "
        f"Agree a convenient time today or tomorrow for {who} to call them back, repeat the "
        "time back to confirm it, then close politely."
    )


def transfer_line(name: str | None) -> str:
    who = first_name(name)
    return (f"Good news, {who} is free now. I'll put you straight through." if who
            else "Good news, a colleague is free now. I'll put you straight through.")


PARALLEL_LINE_COUNTS = (2, 3, 5, 10)


async def parallel_line_cap(entry_ids: list[str]) -> int | None:
    """The adaptive line cap for a batch (Phase 1b), or None when unknown.

    dialer_parallel_line_cap(list) lowers the lines on lists that answer a lot (>=33%
    of the last 50 attempts → 2, >=20% → 3) and by one rung for a team whose extra
    answers too often reach the apology. The strictest list in the batch wins. Fails
    open: the same cap is enforced by a trigger when the session is reserved.
    """
    if not _ready() or not entry_ids:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/cold_call_entries",
                params={"select": "list_id", "id": f"in.({','.join(entry_ids)})"},
                headers=_headers(),
            )
            response.raise_for_status()
            rows = response.json()
            list_ids = sorted({
                str(row["list_id"]) for row in rows
                if isinstance(row, dict) and isinstance(row.get("list_id"), str)
            }) if isinstance(rows, list) else []
            caps: list[int] = []
            for list_id in list_ids:
                capped = await client.post(
                    f"{SUPABASE_URL}/rest/v1/rpc/dialer_parallel_line_cap",
                    json={"p_list_id": list_id}, headers=_headers(),
                )
                capped.raise_for_status()
                value = capped.json()
                if isinstance(value, int) and not isinstance(value, bool):
                    caps.append(value)
        return min(caps) if caps else None
    except Exception as exc:  # noqa: BLE001 - the session trigger still enforces the cap
        logger.warning(f"Parallel line cap unavailable: {type(exc).__name__}")
        return None


def trim_to_line_cap(entry_ids: list[str], cap: int | None) -> list[str]:
    """Keep the first (queue-order) entries that fit the cap, at a supported line count."""
    if not cap or cap >= len(entry_ids):
        return entry_ids
    allowed = [count for count in PARALLEL_LINE_COUNTS if count <= cap]
    return entry_ids[: allowed[-1]] if allowed else entry_ids[:2]
