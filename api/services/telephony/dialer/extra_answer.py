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
ACTIONS = frozenset({"teammate", "agent", "apology", "goodbye", "hangup", "none"})

# A teammate's softphone answers by itself; a join this slow means it did not. Counted
# from after the recording notice when the person hears one first.
OFFER_SECONDS = 3
# How long the spoken recording notice takes before the wait audio starts.
DISCLOSURE_SECONDS = 4
# All teammate offers together stay within about this long; then the chain moves on.
TEAMMATE_TOTAL_SECONDS = 6
# The agent's handover has to land (the person joins the rep's room) within this long.
TRANSFER_LAND_SECONDS = 10
# The agent hands the person back to whoever frees up first within this window.
AGENT_TRANSFER_WINDOW_SECONDS = 90

# Spoken only when the rep has no clip of their own. Short, warm, never "please hold".
NEUTRAL_HELLO = "Hi, one moment."
NEUTRAL_APOLOGY = (
    "Sorry, I've just been pulled onto another call. I'll ring you straight back. Bye for now."
)
# The polite end when a manager switched the apology step off. Never a bare hang-up.
NEUTRAL_GOODBYE = "Sorry we missed you, we'll call you back shortly. Bye for now."
# Repeated (with short pauses, never more than about 2 s of quiet) while a softphone joins.
CONNECTING_LINE = "Just connecting you now."
RECORDING_DISCLOSURE = "This call may be recorded and monitored for quality assurance."
# 24 Oct: no automatic recording notice when a person is put through to a rep on an
# outbound call. The rep says it themselves; a robotic line at the start of every call
# cost the conversation. AI-agent hand-offs and inbound calls keep theirs (no rep to say it).
AUTO_RECORDING_NOTICE = False

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


def offer_seconds(*, disclose: bool) -> int:
    """How long an offer may ring: about 3 s, counted from after the recording notice."""
    return OFFER_SECONDS + (DISCLOSURE_SECONDS if disclose and AUTO_RECORDING_NOTICE else 0)


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
    conference_name: str | None = None, offer_seconds: int = OFFER_SECONDS,
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
        "p_offer_seconds": int(offer_seconds),
    })
    if state is None or state.get("action") not in ACTIONS:
        return None
    return state


async def mark(
    provider: str, prospect_call_id: str, event: str, detail: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if event not in {
        "rep_joined", "agent_started", "agent_transferred", "callback_requested", "ended",
        "transfer_recovery",
    }:
        return None
    return await _rpc_dict("dialer_extra_answer_mark", {
        "p_provider": provider, "p_prospect_call_id": prospect_call_id,
        "p_event": event, "p_detail": detail or {},
    })


async def prospect_joined(*, provider: str, prospect_call_id: str) -> dict[str, Any] | None:
    """The person reached the rep's room after the agent's handover: the transfer landed."""
    return await _rpc_dict("dialer_extra_answer_prospect_joined", {
        "p_provider": provider, "p_prospect_call_id": prospect_call_id,
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
    if not path.lower().endswith(".wav"):
        # Phone playback needs WAV, exactly like the voicemail resolver.
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
        "time back to confirm it, and once they confirm it call book_callback with the agreed "
        "time. Then close politely."
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


# ── the agent's summary for the rep's card ─────────────────────────────────────────────

_TIME_RE = re.compile(
    r"\b(?:(?:today|tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"(?:\s+(?:morning|afternoon|evening))?(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?"
    r"|\d{1,2}(?::\d{2})?\s*(?:am|pm)|\d{1,2}:\d{2}|(?:in\s+)?(?:an?|\d+)\s+(?:hour|minute)s?)\b",
    re.I,
)
_NAME_RE = re.compile(r"\b(?i:my name is|this is|i'?m|it'?s)\s+([A-Z][a-z'-]{1,20})\b")
_COMPANY_RE = re.compile(
    r"\b(?i:i'?m|we'?re|i work|calling)\s+(?i:with|at|from|for)\s+([A-Z][\w&'-]*(?:\s+[A-Z][\w&'-]*){0,3})")
_INTEREST_RE = re.compile(
    r"\b(interested|sounds good|tell me more|send (?:me )?(?:some )?(?:info|information|details)|"
    r"how much|what does it cost|pricing|yes please|that would be great)\b", re.I)
_OBJECTION_RE = re.compile(
    r"\b(not interested|too expensive|no budget|already (?:have|use|got)|busy right now|"
    r"bad time|call (?:me )?(?:back )?later|don'?t need|remove me|stop calling)\b", re.I)
_NOT_NAMES = {"Sorry", "Just", "Not", "Calling", "Busy", "Here", "Fine", "Good", "Great", "Ok", "Okay", "Yes", "No"}


def _turns(messages: Any) -> list[tuple[str, str]]:
    turns: list[tuple[str, str]] = []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
        text = " ".join(str(content or "").split())
        if text and not text.startswith("[Private instruction"):
            turns.append((str(message["role"]), text))
    return turns


def summarize_conversation(messages: Any, *, limit: int = 900) -> str:
    """Key facts from the agent's conversation for the rep's card, deterministic.

    Name, company, interest, objections, an agreed time and their last question. Falls
    back to their last two lines when nothing stands out. Never the agent's own patter.
    """
    turns = _turns(messages)
    theirs = [text for role, text in turns if role == "user"]
    if not theirs:
        return ""
    said = " ".join(theirs)
    facts: list[str] = []
    name = next((m.group(1) for m in _NAME_RE.finditer(said) if m.group(1) not in _NOT_NAMES), None)
    if name:
        facts.append(f"Name: {name}")
    company = _COMPANY_RE.search(said)
    if company:
        facts.append(f"Company: {company.group(1)}")
    interest = sorted({m.group(1).lower() for m in _INTEREST_RE.finditer(said)})
    if interest:
        facts.append("Interest: " + ", ".join(interest[:3]))
    objections = sorted({m.group(1).lower() for m in _OBJECTION_RE.finditer(said)})
    if objections:
        facts.append("Objections: " + ", ".join(objections[:3]))
    times = [m.group(0) for text in theirs for m in _TIME_RE.finditer(text)]
    if times:
        facts.append(f"Time mentioned: {times[-1]}")
    question = next((text for text in reversed(theirs) if "?" in text), None)
    if question:
        facts.append(f"Last question: {question[-200:]}")
    if not facts:
        facts.append("They said: " + " / ".join(theirs[-2:])[-400:])
    return "\n".join(facts)[:limit]


def _summary_llm_enabled() -> bool:
    import os
    return (os.environ.get("SYSEVO_DIALER_SUMMARY_LLM") or "").strip().lower() in {"1", "on", "true", "yes"} and bool(
        (os.environ.get("OPENAI_API_KEY") or "").strip())


async def agent_summary(messages: Any, *, budget_seconds: float = 3.0) -> str:
    """A short summary for the rep: an LLM when switched on (3 s budget), else key facts."""
    facts = summarize_conversation(messages)
    if not facts or not _summary_llm_enabled():
        return facts
    import asyncio
    import os
    transcript = "\n".join(
        ("Them: " if role == "user" else "Agent: ") + text for role, text in _turns(messages)[-16:]
    )[-4000:]

    async def ask() -> str:
        async with httpx.AsyncClient(timeout=budget_seconds) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY'].strip()}"},
                json={
                    "model": os.environ.get("SYSEVO_DIALER_SUMMARY_MODEL") or "gpt-4o-mini",
                    "max_tokens": 160, "temperature": 0,
                    "messages": [
                        {"role": "system", "content": (
                            "Summarise this sales call for the rep taking it over, in at most 4 short "
                            "lines: who they are, what they want or object to, any agreed time, and "
                            "their last open question. Facts only, from the transcript.")},
                        {"role": "user", "content": transcript},
                    ],
                },
            )
            response.raise_for_status()
            return str(response.json()["choices"][0]["message"]["content"] or "").strip()

    try:
        text = await asyncio.wait_for(ask(), timeout=budget_seconds)
    except Exception as exc:  # noqa: BLE001 - the key facts are always there
        logger.info(f"Agent summary LLM unavailable: {type(exc).__name__}")
        return facts
    return text[:900] if text else facts
