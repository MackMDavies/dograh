"""Supabase writes for the dialer call log.

dialer_calls lives entirely in Supabase (see
docs/superpowers/specs/2026-08-11-dialer-call-log-recording-design.md in
the sysevo repo) - same reasoning as dialer_number_assignment.py: no local
copy of this data in Dograh, so every write is a server-to-server Supabase
REST call using the service-role key.

Every function here is fail-soft: it must never raise. These run inside
Twilio webhook handlers AFTER Twilio already has what it needs to keep the
call running - a write failure here means a dialer_calls row goes
stale/unfilled, never a broken call.
"""

import re
from datetime import UTC, datetime
from typing import Any

import httpx
from loguru import logger

from api.constants import SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
from api.services.telephony.dialer.compliance import (
    calling_hours_allowed,
    calling_hours_result,
    phone_block_reason,
)

_DIALER_CALLS_URL_SUFFIX = "/rest/v1/dialer_calls"

# The carrier is done with the leg. ALL of these end a call - not just "completed",
# which is what this module used to test and is why a cancelled or unanswered call
# settled in Supabase with a status but no end time. Anything reading `ended_at is
# null` then read those rows as calls in progress: a manager's Live tab showed two
# calls that had hung up half an hour earlier, with running timers and a Listen
# button each. Mirrors dialer_terminal_statuses() in the Supabase schema and
# DIALER_ENDED_STATUSES in the sysevo frontend.
_TERMINAL_CALL_STATUSES = frozenset(
    {"completed", "busy", "failed", "no-answer", "canceled"}
)


def _headers() -> dict:
    return {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


async def create_dialer_call(
    *,
    parent_call_sid: str,
    rep_user_id: str,
    entry_id: str | None,
    from_number: str,
    to_number: str,
    provider: str = "twilio",
    phone_us_state: str | None = None,
    double_dial_retry_of: str | None = None,
    double_dial_authorization_id: str | None = None,
) -> bool:
    """Create the initial dialer_calls row synchronously, before voice-connect
    returns TwiML - see the module docstring on why this can't wait for an
    async callback.

    ``provider`` defaults to "twilio" so the existing Twilio call sites keep
    working untouched; the SignalWire dialer webhook passes "signalwire"
    explicitly. It matters downstream because the recording URL for a
    SignalWire call is stored directly (recording_url) while Twilio's is a
    SID the frontend proxies (recording_sid).
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_SERVICE_ROLE_KEY not set - cannot create dialer_calls row")
        return False
    payload = {
        "parent_call_sid": parent_call_sid,
        "rep_user_id": rep_user_id,
        "entry_id": entry_id,
        "from_number": from_number,
        "to_number": to_number,
        "status": "initiated",
        "provider": provider,
    }
    if phone_us_state:
        payload["phone_us_state"] = phone_us_state
    if double_dial_retry_of:
        payload["double_dial_retry_of"] = double_dial_retry_of
    if double_dial_authorization_id:
        payload["double_dial_authorization_id"] = double_dial_authorization_id
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}{_DIALER_CALLS_URL_SUFFIX}",
                json=payload,
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            return True
    except Exception as exc:  # noqa: BLE001 - deliberate: this module's whole contract is "never raise"
        logger.error(f"Failed to create dialer_calls row for {parent_call_sid}: {exc}")
        return False


async def consume_double_dial_authorization(
    *,
    authorization_id: str,
    rep_user_id: str,
    entry_id: str,
    to_number: str,
    call_sid: str,
) -> dict[str, str] | None:
    """Consume the one-time server grant and return its pinned number/source."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_SERVICE_ROLE_KEY not set - cannot validate double-dial authorization")
        return None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/dialer_consume_double_dial_authorization",
                json={
                    "p_authorization_id": authorization_id,
                    "p_rep_user_id": rep_user_id,
                    "p_entry_id": entry_id,
                    "p_phone": to_number,
                    "p_call_sid": call_sid,
                },
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
                return None
            row = rows[0]
            from_number = row.get("from_number")
            retry_of_call_id = row.get("retry_of_call_id")
            if not isinstance(from_number, str) or not from_number or not isinstance(retry_of_call_id, str):
                return None
            return {"from_number": from_number, "retry_of_call_id": retry_of_call_id}
    except Exception as exc:  # noqa: BLE001 - fail closed; this is call authorization
        logger.error(f"Failed to consume double-dial authorization {authorization_id}: {exc}")
        return None


async def update_dialer_call_status(
    *,
    parent_call_sid: str,
    child_call_sid: str | None,
    status: str,
    duration_seconds: int | None,
    ended_by: str | None = None,
    rep_user_id: str | None = None,
    provider: str | None = None,
) -> None:
    """Update status/duration from the <Number>'s statusCallback.

    ``ended_by`` ('rep' | 'prospect') only when the provider said which side hung up;
    omitted otherwise, for the same reason as every other key below.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_SERVICE_ROLE_KEY not set - cannot update dialer_calls status")
        return
    # Only the fields this callback actually knows. A PATCH that always sends every
    # key writes null for the ones it has nothing for, and null in a PATCH is an
    # instruction to ERASE - so a "no-answer" callback carrying no duration wiped a
    # real duration another callback had already recorded, and the old unconditional
    # ended_at wiped a real end time the same way. Omitting a key leaves the column
    # untouched, which is what "I have nothing to say about this" should mean.
    payload: dict[str, object] = {"status": status}
    if child_call_sid is not None:
        payload["child_call_sid"] = child_call_sid
    if duration_seconds is not None:
        payload["duration_seconds"] = duration_seconds
    if ended_by is not None:
        payload["ended_by"] = ended_by
    if status in _TERMINAL_CALL_STATUSES:
        # now(), not started_at + duration: this callback arrives at the hangup, and
        # duration counts talk time only - a call that rang for twenty seconds before
        # anyone answered genuinely ended later than start + duration implies. The
        # Supabase trigger derives that lower bound as a backstop when nothing else
        # supplies one; this is the better number, so it goes in first.
        payload["ended_at"] = datetime.now(UTC).isoformat()

    try:
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{SUPABASE_URL}{_DIALER_CALLS_URL_SUFFIX}",
                params={
                    "parent_call_sid": f"eq.{parent_call_sid}",
                    **({"rep_user_id": f"eq.{rep_user_id}"} if rep_user_id else {}),
                    **({"provider": f"eq.{provider}"} if provider else {}),
                },
                json=payload,
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - deliberate: this module's whole contract is "never raise"
        logger.error(f"Failed to update dialer_calls status for {parent_call_sid}: {exc}")


async def update_dialer_call_conference_sid(*, parent_call_sid: str, conference_sid: str) -> None:
    """Populates conference_sid once the Conference actually starts (the
    rep has joined) - this is later used to correlate the Conference's own
    recording callback, which identifies via ConferenceSid rather than any
    participant's CallSid."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_SERVICE_ROLE_KEY not set - cannot update dialer_calls conference_sid")
        return
    try:
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{SUPABASE_URL}{_DIALER_CALLS_URL_SUFFIX}",
                params={"parent_call_sid": f"eq.{parent_call_sid}"},
                json={"conference_sid": conference_sid},
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - deliberate: this module's whole contract is "never raise"
        logger.error(f"Failed to update dialer_calls conference_sid for {parent_call_sid}: {exc}")


async def update_dialer_call_child_sid(*, parent_call_sid: str, child_call_sid: str) -> None:
    """Record the lead leg's SID as soon as the outbound dial returns.

    update_dialer_call_status also writes this column, but only when the
    lead's own status callback arrives - which the conference-end orphan
    cleanup can outrace, leaving it with no SID to end the leg by. This
    patches child_call_sid alone, deliberately NOT status, so it can never
    walk the row's status backwards past a callback that already landed.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_SERVICE_ROLE_KEY not set - cannot update dialer_calls child_call_sid")
        return
    try:
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{SUPABASE_URL}{_DIALER_CALLS_URL_SUFFIX}",
                params={"parent_call_sid": f"eq.{parent_call_sid}"},
                json={"child_call_sid": child_call_sid},
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - deliberate: this module's whole contract is "never raise"
        logger.error(f"Failed to update dialer_calls child_call_sid for {parent_call_sid}: {exc}")


async def update_signalwire_dialer_amd(
    *, parent_call_sid: str, amd_result: str | None = None,
    voicemail_started: bool = False, voicemail_sent: bool = False,
) -> bool:
    """Persist machine-detection and voicemail playback state for one leg."""
    allowed = {
        "human", "machine_start", "machine_end_beep", "machine_end_silence",
        "machine_end_other", "fax", "unknown",
    }
    if (amd_result is not None and amd_result not in allowed) or not parent_call_sid:
        return False
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return False
    payload: dict[str, object] = {}
    if amd_result is not None:
        payload["amd_result"] = amd_result
    if voicemail_started:
        payload["voicemail_drop_started_at"] = datetime.now(UTC).isoformat()
    if voicemail_sent:
        payload["auto_voicemail_sent"] = True
    if not payload:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.patch(
                f"{SUPABASE_URL}{_DIALER_CALLS_URL_SUFFIX}",
                params={
                    "parent_call_sid": f"eq.{parent_call_sid}",
                    "provider": "eq.signalwire",
                },
                json=payload,
                headers=_headers(),
            )
            response.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 - a history write must not interrupt provider media
        logger.error(f"Failed to persist SignalWire AMD for {parent_call_sid}: {exc}")
        return False


async def get_dialer_call_child_leg(*, parent_call_sid: str) -> dict | None:
    """Read back the lead leg's SID and last-known status.

    The only read in this module. dialer-conference-events uses it to decide
    whether the lead's outbound leg was left orphaned (still ringing, never
    a conference participant) when the conference ended.

    Returns None - never raises, never partially reports - when the row is
    missing or unreadable for any reason, so callers treat "don't know" and
    "nothing there" identically.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_SERVICE_ROLE_KEY not set - cannot read dialer_calls row")
        return None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SUPABASE_URL}{_DIALER_CALLS_URL_SUFFIX}",
                params={
                    "select": "child_call_sid,status,rep_user_id,provider,amd_result,manual_voicemail_requested_at,voicemail_drop_started_at,auto_voicemail_sent,voicemail_drop_id,ended_at,lead_joined_at",
                    "parent_call_sid": f"eq.{parent_call_sid}",
                    "limit": "1",
                },
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            rows = response.json()
    except Exception as exc:  # noqa: BLE001 - deliberate: this module's whole contract is "never raise"
        logger.error(f"Failed to read dialer_calls row for {parent_call_sid}: {exc}")
        return None
    # A 200 from a PostgREST table GET is always a JSON array of objects
    # (error bodies are objects, but raise_for_status has already rejected
    # those, and we don't send the vnd.pgrst.object+json Accept header). The
    # shape check is for what sits BETWEEN us and PostgREST - a proxy or
    # captive portal answering 200 with a body that isn't ours at all - since
    # "never raises" has to hold for that too. Both halves earn their keep:
    # a non-list body is caught by the first, and a list of NON-objects
    # (["error"], [123], [["nested"]]) only by the second. Returning one of
    # those would break this function's dict|None contract and blow up in the
    # caller's .get() instead, outside any of this module's error handling.
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0]
    return None


async def get_team_voicemail_policy(
    *, user_id: str, parent_call_sid: str | None = None, entry_id: str | None = None
) -> dict[str, Any] | None:
    """Read the server-owned account voicemail/AMD policy, failing closed."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/dialer_account_voicemail_policy",
                json={
                    "p_rep_user_id": user_id,
                    "p_entry_id": entry_id,
                    "p_parent_call_sid": parent_call_sid,
                },
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            policy = response.json()
            return policy if isinstance(policy, dict) else None
    except Exception as exc:  # noqa: BLE001 - callers need safe defaults
        logger.warning(f"Could not resolve team voicemail policy for {user_id}: {exc}")
        return None


async def get_dialer_phone_block_reason(phone: str) -> str | None:
    """Use the server-owned suppression register before a provider originates."""
    return await phone_block_reason(phone)


async def check_dialer_calling_hours(phone: str, entry_id: str | None = None) -> tuple[bool, str | None]:
    """Ask the server-owned timezone/rule engine before placing a PSTN leg.

    The frontend's prospect clock is informational. Unknown timezone or an
    unavailable compliance service must not turn into permission to call.
    """
    return await calling_hours_allowed(phone, entry_id)


async def check_dialer_calling_hours_with_state(
    phone: str, entry_id: str | None = None
) -> tuple[bool, str | None, str | None]:
    """Return the fail-closed dial decision and the resolved destination state."""
    return await calling_hours_result(phone, entry_id)


_UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# Recording durations seen by this worker, keyed by drop id. Used only to sign
# the expected playback length into the completion callback URL; a cold cache
# just means the completion check falls back to "no expectation".
_DROP_DURATIONS: dict[str, float] = {}
_DROP_DURATIONS_MAX = 512


def remember_voicemail_duration(drop_id: str | None, seconds: Any) -> None:
    if not drop_id:
        return
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return
    if not (0 < value <= 120):
        return
    if len(_DROP_DURATIONS) >= _DROP_DURATIONS_MAX:
        _DROP_DURATIONS.pop(next(iter(_DROP_DURATIONS)))
    _DROP_DURATIONS[str(drop_id)] = value


def voicemail_duration(drop_id: str | None) -> float | None:
    return _DROP_DURATIONS.get(str(drop_id)) if drop_id else None


def _owned_voicemail_path(path: Any, owner_user_id: Any) -> bool:
    """A recording path must sit directly in its owner's storage folder."""
    if not isinstance(path, str) or not isinstance(owner_user_id, str):
        return False
    return bool(re.fullmatch(rf"{re.escape(owner_user_id)}/[^/]+", path))


def is_provider_playable_voicemail(path_or_url: str | None) -> bool:
    """Twilio <Play> and SignalWire play accept WAV/MP3, not browser WebM/Opus."""
    if not path_or_url:
        return False
    return path_or_url.split("?", 1)[0].lower().endswith((".wav", ".mp3"))


async def _legacy_own_default_drop(client: httpx.AsyncClient, rep_user_id: str) -> dict | None:
    response = await client.get(
        f"{SUPABASE_URL}/rest/v1/dialer_voicemail_drops",
        params={
            "select": "id,storage_path,duration_seconds",
            "user_id": f"eq.{rep_user_id}",
            "is_default": "eq.true",
            "limit": "1",
        },
        headers=_headers(),
        timeout=5.0,
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    return {
        "drop_id": rows[0].get("id"),
        "storage_path": rows[0].get("storage_path"),
        "duration_seconds": rows[0].get("duration_seconds"),
        "owner_user_id": rep_user_id,
        "source": "own_default",
    }


async def resolve_effective_voicemail_drop(
    *, rep_user_id: str, entry_id: str | None = None, parent_call_sid: str | None = None,
) -> dict | None:
    """Return the recording a call will actually play, or None.

    One server resolver for BOTH providers: an authorized explicit team
    selection, then the rep's own default, then the account's team default
    (`dialer_effective_voicemail_drop`). Before that RPC exists the old
    owner-folder default is used, so a deploy ahead of the migration keeps
    today's behaviour. Raises on transport errors; callers fail closed.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY or not rep_user_id:
        return None
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/dialer_effective_voicemail_drop",
            json={
                "p_rep_user_id": rep_user_id,
                "p_entry_id": entry_id,
                "p_parent_call_sid": parent_call_sid,
            },
            headers=_headers(),
        )
        if response.status_code == 404:
            logger.warning("dialer_effective_voicemail_drop is not deployed; using the rep's own default")
            row = await _legacy_own_default_drop(client, rep_user_id)
        else:
            response.raise_for_status()
            row = response.json()
    if not isinstance(row, dict):
        return None
    drop_id = str(row.get("drop_id") or "")
    owner = str(row.get("owner_user_id") or "")
    path = row.get("storage_path")
    if not _UUID_PATTERN.fullmatch(drop_id) or not _owned_voicemail_path(path, owner):
        logger.warning(f"Rejected an unsafe voicemail recording reference for {rep_user_id}")
        return None
    remember_voicemail_duration(drop_id, row.get("duration_seconds"))
    return {
        "id": drop_id,
        "storage_path": path,
        "duration_seconds": row.get("duration_seconds"),
        "owner_user_id": owner,
        "source": str(row.get("source") or "own_default"),
    }


async def verify_voicemail_audio(url: str) -> bool | None:
    """Check the signed object really is a RIFF/WAVE (or MP3) file.

    True: playable header. False: the provider would fetch something it
    cannot play (missing object, HTML error page, browser container renamed).
    None: could not check (network) - callers proceed and rely on the
    completion timing check rather than refusing a valid voicemail.
    """
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            async with client.stream("GET", url, headers={"Range": "bytes=0-15"}) as response:
                if response.status_code in (401, 403, 404, 410):
                    return False
                if response.status_code not in (200, 206):
                    return None
                head = b""
                async for chunk in response.aiter_bytes():
                    head += chunk
                    if len(head) >= 12:
                        break
    except Exception as exc:  # noqa: BLE001 - verification is advisory on transport errors
        logger.info(f"Could not verify voicemail audio header: {type(exc).__name__}")
        return None
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return True
    if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return True
    return False


async def sign_voicemail_storage_path(storage_path: str, *, expires_in: int = 1800) -> str | None:
    """Mint a short-lived URL for a private recording (service role only)."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    async with httpx.AsyncClient(timeout=5.0) as client:
        signed = await client.post(
            f"{SUPABASE_URL}/storage/v1/object/sign/voicemail-drops/{storage_path}",
            json={"expiresIn": expires_in},
            headers=_headers(),
        )
        signed.raise_for_status()
        signed_path = signed.json().get("signedURL")
    if not isinstance(signed_path, str) or not signed_path.startswith("/"):
        return None
    storage_path_part = signed_path if signed_path.startswith("/storage/v1/") else f"/storage/v1{signed_path}"
    return f"{SUPABASE_URL}{storage_path_part}"


def effective_auto_send(policy: dict[str, Any] | None, *, parallel: bool = False) -> bool:
    """Whether this call's machine answer gets the recording without the rep.

    Resolved on the server from ``dialer_account_voicemail_policy`` for the
    call's own rep_user_id (never from the browser):

    * single call  -> ``single_auto_send``: the rep's own "Send my voicemail
      automatically" choice when they have made one, else the team default,
      and only while the team runs machine detection with action drop_vm;
    * parallel line -> ``parallel_auto_send``: every machine line is dropped
      whenever the team action is drop_vm, whatever the single-call choice.

    Before migration 20270421110000 the policy has neither key; the old team
    switch (``auto_drop_enabled``) then decides, exactly as before.
    """
    if not isinstance(policy, dict):
        return False
    key = "parallel_auto_send" if parallel else "single_auto_send"
    if key in policy:
        return policy.get(key) is True
    return policy.get("auto_drop_enabled") is True


async def get_auto_voicemail_settings(
    *,
    user_id: str,
    allow_manual_when_disabled: bool = False,
    parent_call_sid: str | None = None,
    entry_id: str | None = None,
    parallel: bool = False,
) -> tuple[bool, str | None, str | None]:
    """Return team policy and a short-lived URL for the effective recording.

    The bucket is private. The URL is minted with the service role only after
    checking the account-owned automatic-drop policy and resolving the
    recording on the server (explicit team choice → rep default → team
    default). Whether it plays automatically is effective_auto_send(): the
    rep's own choice (or the team default) for a single call, the team's
    on-machine action for a parallel line.
    A recording whose bytes are not a playable WAV/MP3 is refused here, so a
    silent "sent" can never be reported for a file the carrier cannot play.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return False, None, None
    try:
        team_policy = await get_team_voicemail_policy(
            user_id=user_id, parent_call_sid=parent_call_sid, entry_id=entry_id
        )
        enabled = effective_auto_send(team_policy, parallel=parallel)
        if not enabled and not allow_manual_when_disabled:
            return False, None, None
        drop = await resolve_effective_voicemail_drop(
            rep_user_id=user_id, entry_id=entry_id, parent_call_sid=parent_call_sid,
        )
        if not drop:
            return False, None, None
        url = await sign_voicemail_storage_path(drop["storage_path"], expires_in=1800)
        if not url:
            return False, None, None
        if is_provider_playable_voicemail(url) and await verify_voicemail_audio(url) is False:
            logger.warning(f"Voicemail recording {drop['id']} is not playable audio; not sending it")
            return False, None, None
        return True, url, drop["id"]
    except Exception as exc:  # noqa: BLE001 - optional automation must fail closed
        logger.warning(f"Could not resolve automatic voicemail for {user_id}: {exc}")
        return False, None, None


async def attach_voicemail_drop_to_call(
    *, parent_call_sid: str, drop_id: str, child_call_sid: str | None = None,
) -> bool:
    """Persist which exact recording was selected for this playback attempt."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY or not parent_call_sid or not drop_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            headers = {**_headers(), "Prefer": "return=representation"}
            rows_updated = 0
            if child_call_sid:
                for url in (
                    f"{SUPABASE_URL}/rest/v1/dialer_parallel_legs",
                    f"{SUPABASE_URL}{_DIALER_CALLS_URL_SUFFIX}",
                ):
                    response = await client.patch(
                        url,
                        params={
                            "parent_call_sid": f"eq.{parent_call_sid}",
                            "child_call_sid": f"eq.{child_call_sid}",
                        },
                        json={"voicemail_drop_id": drop_id},
                        headers=headers,
                    )
                    response.raise_for_status()
                    rows_updated += len(response.json()) if response.content else 0
            else:
                response = await client.patch(
                    f"{SUPABASE_URL}{_DIALER_CALLS_URL_SUFFIX}",
                    params={"parent_call_sid": f"eq.{parent_call_sid}"},
                    json={"voicemail_drop_id": drop_id},
                    headers=headers,
                )
                response.raise_for_status()
                rows_updated += len(response.json()) if response.content else 0
        return rows_updated > 0
    except Exception as exc:  # noqa: BLE001 - analytics linkage cannot block a valid call
        logger.warning(f"Could not associate voicemail drop with {parent_call_sid}: {exc}")
        return False


async def update_dialer_call_amd_result(*, parent_call_sid: str, answered_by: str) -> bool:
    """Persist the signed Twilio AMD result before deciding whether to bridge."""
    allowed = {"human", "machine_start", "machine_end_beep", "machine_end_silence", "machine_end_other", "fax", "unknown"}
    value = answered_by.strip().lower()
    if value not in allowed or not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{SUPABASE_URL}{_DIALER_CALLS_URL_SUFFIX}",
                params={"parent_call_sid": f"eq.{parent_call_sid}", "provider": "eq.twilio"},
                json={"amd_result": value},
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 - AMD write failure must not break the signed call webhook
        logger.error(f"Failed to persist AMD result for {parent_call_sid}: {exc}")
        return False


async def request_manual_voicemail_drop(
    *, parent_call_sid: str, rep_user_id: str, cancel: bool = False
) -> dict | None:
    """Set or cancel the authenticated rep's pending Twilio voicemail request."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/dialer_request_manual_voicemail_drop",
                json={
                    "p_parent_call_sid": parent_call_sid,
                    "p_rep_user_id": rep_user_id,
                    "p_cancel": cancel,
                },
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            result = response.json()
        return result if isinstance(result, dict) else None
    except Exception as exc:  # noqa: BLE001 - no ambiguous accept on a live call
        logger.error(f"Could not request manual voicemail for {parent_call_sid}: {exc}")
        return None


async def claim_manual_voicemail_playback(
    *, parent_call_sid: str, child_call_sid: str, manual: bool = True
) -> bool:
    """Atomically ensure the API and AMD webhook cannot both start the same drop."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/dialer_claim_manual_voicemail_playback",
                json={
                    "p_parent_call_sid": parent_call_sid,
                    "p_child_call_sid": child_call_sid,
                    "p_manual": manual,
                },
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            return response.json() is True
    except Exception as exc:  # noqa: BLE001 - claim uncertainty must fail closed
        logger.error(f"Could not claim manual voicemail playback for {parent_call_sid}: {exc}")
        return False


async def release_manual_voicemail_playback(*, parent_call_sid: str, child_call_sid: str) -> bool:
    """Release the one-shot claim if Twilio rejected the requested redirect."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/dialer_release_manual_voicemail_playback",
                json={"p_parent_call_sid": parent_call_sid, "p_child_call_sid": child_call_sid},
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            return response.json() is True
    except Exception as exc:  # noqa: BLE001 - do not hide a failed retry release
        logger.error(f"Could not release manual voicemail playback claim for {parent_call_sid}: {exc}")
        return False


async def mark_dialer_lead_joined_conference(*, parent_call_sid: str, child_call_sid: str) -> bool:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/dialer_mark_lead_joined_conference",
                json={"p_parent_call_sid": parent_call_sid, "p_child_call_sid": child_call_sid},
                headers=_headers(),
                timeout=5.0,
            )
            response.raise_for_status()
            return response.json() is True
    except Exception as exc:  # noqa: BLE001 - missing this signal only disables manual redirect fallback
        logger.error(f"Could not mark lead conference join for {parent_call_sid}: {exc}")
        return False


async def mark_auto_voicemail_sent(*, parent_call_sid: str, child_call_sid: str | None = None) -> bool:
    """Record completed playback on a single call or its parallel child leg."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return False
    try:
        async with httpx.AsyncClient() as client:
            if child_call_sid:
                response = await client.patch(
                    f"{SUPABASE_URL}/rest/v1/dialer_parallel_legs",
                    params={
                        "parent_call_sid": f"eq.{parent_call_sid}",
                        "child_call_sid": f"eq.{child_call_sid}",
                    },
                    json={"auto_voicemail_sent": True},
                    headers=_headers(),
                    timeout=5.0,
                )
            else:
                response = await client.patch(
                    f"{SUPABASE_URL}{_DIALER_CALLS_URL_SUFFIX}",
                    params={"parent_call_sid": f"eq.{parent_call_sid}"},
                    json={"auto_voicemail_sent": True},
                    headers=_headers(),
                    timeout=5.0,
                )
            response.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 - status evidence must not break webhook
        logger.error(f"Failed to record automatic voicemail for {parent_call_sid}: {exc}")
        return False


async def _service_rpc(name: str, payload: dict) -> Any:
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/{name}", json=payload, headers=_headers(),
        )
        response.raise_for_status()
        return response.json()


async def record_twilio_amd_decision(
    *, parent_call_sid: str, decision_ms: Any, child_call_sid: str | None = None,
    leg_id: str | None = None,
) -> bool:
    """Persist Twilio's measured MachineDetectionDuration (first decision wins)."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY or not parent_call_sid:
        return False
    try:
        ms = int(str(decision_ms).strip())
    except (TypeError, ValueError):
        return False
    if ms < 0:
        return False
    try:
        return await _service_rpc("dialer_record_amd_decision", {
            "p_parent_call_sid": parent_call_sid,
            "p_decision_ms": min(ms, 120_000),
            "p_child_call_sid": child_call_sid,
            "p_leg_id": leg_id,
        }) is True
    except Exception as exc:  # noqa: BLE001 - diagnostics must never affect the call
        logger.warning(f"Could not record AMD decision time for {parent_call_sid}: {exc}")
        return False


async def record_voicemail_playback_failure(
    *, parent_call_sid: str, reason: str, child_call_sid: str | None = None,
    leg_id: str | None = None,
) -> bool:
    """A started playback did not reach its end; never report it as sent."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY or not parent_call_sid:
        return False
    try:
        return await _service_rpc("dialer_record_voicemail_playback_failure", {
            "p_parent_call_sid": parent_call_sid,
            "p_reason": reason[:120],
            "p_child_call_sid": child_call_sid,
            "p_leg_id": leg_id,
        }) is True
    except Exception as exc:  # noqa: BLE001 - evidence write must not break the webhook
        logger.error(f"Could not record voicemail playback failure for {parent_call_sid}: {exc}")
        return False


INTERRUPTED_PLAYBACK_OUTCOMES = frozenset({"vm_left", "mailbox_full", "cut_short"})


async def settle_interrupted_playback(
    *, parent_call_sid: str, child_call_sid: str | None = None, leg_id: str | None = None,
) -> str | None:
    """The carrier ended a call while the recording was playing: give it a final outcome.

    dialer_settle_interrupted_playback measures how much played against the recording's
    own length: 'vm_left' (it reached the end), 'mailbox_full' (ended as it began) or
    'cut_short'. It writes the failure/sent flag, the stage and the lead's disposition.
    None when nothing was playing or it was already settled. Never raises.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY or not parent_call_sid:
        return None
    try:
        result = await _service_rpc("dialer_settle_interrupted_playback", {
            "p_parent_call_sid": parent_call_sid,
            "p_child_call_sid": child_call_sid,
            "p_leg_id": leg_id or None,
        })
    except Exception as exc:  # noqa: BLE001 - evidence write must not break the webhook
        logger.error(f"Could not settle an interrupted playback for {parent_call_sid}: {exc}")
        return None
    return result if result in INTERRUPTED_PLAYBACK_OUTCOMES else None


async def mark_rep_detached_for_voicemail(*, parent_call_sid: str) -> bool:
    """True when a queued manual voicemail should keep the ringing leg alive."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY or not parent_call_sid:
        return False
    try:
        return await _service_rpc(
            "dialer_mark_rep_detached_for_voicemail", {"p_parent_call_sid": parent_call_sid}
        ) is True
    except Exception as exc:  # noqa: BLE001 - unknown means cancel as before (fail safe)
        logger.warning(f"Could not keep {parent_call_sid} alive for a queued voicemail: {exc}")
        return False


async def get_voicemail_drop_evidence(*, parent_call_sid: str) -> dict:
    """Read failure/detach evidence separately from the core call row.

    Kept out of get_dialer_call_child_leg's select on purpose: if this backend
    ships before the migration adds these columns, only this optional read
    fails (returning {}), never the call row every webhook depends on.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY or not parent_call_sid:
        return {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{SUPABASE_URL}{_DIALER_CALLS_URL_SUFFIX}",
                params={
                    "select": "voicemail_drop_failed_at,voicemail_drop_failure,rep_detached_at,amd_ms",
                    "parent_call_sid": f"eq.{parent_call_sid}",
                    "limit": "1",
                },
                headers=_headers(),
            )
            response.raise_for_status()
            rows = response.json()
    except Exception as exc:  # noqa: BLE001 - optional evidence
        logger.info(f"Voicemail evidence unavailable for {parent_call_sid}: {type(exc).__name__}")
        return {}
    return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}


async def update_dialer_call_recording_url(
    *, parent_call_sid: str, recording_url: str
) -> None:
    """Store a directly-playable recording URL.

    The SignalWire counterpart to update_dialer_call_recording_by_conference_sid,
    and it correlates on parent_call_sid rather than conference_sid for a
    concrete reason: SignalWire records the CALL, not a conference, so its
    recording callback carries our own call id and needs no second webhook
    to have populated a correlation column first. That also means there is
    no silent-miss failure mode worth a count=exact check here.

    Writes recording_url, not recording_sid: SignalWire hands back a URL we
    can hand straight to an <audio> tag, whereas Twilio's SID has to go
    through the recording proxy.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_SERVICE_ROLE_KEY not set - cannot update dialer_calls recording_url")
        return
    row_id: str | None = None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{SUPABASE_URL}{_DIALER_CALLS_URL_SUFFIX}",
                params={
                    "parent_call_sid": f"eq.{parent_call_sid}",
                    # The row id is what the transcriber addresses work by, and this is
                    # the only moment we hold both the provider's id and ours.
                    "select": "id",
                },
                json={"recording_url": recording_url},
                headers={**_headers(), "Prefer": "return=representation"},
                timeout=5.0,
            )
            response.raise_for_status()
            rows = response.json()
            if isinstance(rows, list) and rows:
                row_id = str(rows[0].get("id") or "") or None
    except Exception as exc:  # noqa: BLE001 - deliberate: this module's whole contract is "never raise"
        logger.error(f"Failed to update dialer_calls recording_url for {parent_call_sid}: {exc}")
        return

    if row_id:
        await _kick_transcription(row_id)


async def _kick_transcription(dialer_call_id: str) -> None:
    """Start transcription the moment the recording lands.

    The cron sweep already guarantees every recording is eventually transcribed, but
    "eventually" is up to two minutes, and a rep who has just hung up expects the call to
    be there. This is the fast path; the sweep stays as the safety net for a recording
    whose callback never arrives or whose kick fails.

    Fire-and-forget by design: a rep's call must never fail because a transcription could
    not be started, and the sweep will pick it up regardless.
    """
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{SUPABASE_URL}/functions/v1/dialer-call-transcribe",
                json={"dialer_call_id": dialer_call_id},
                headers={
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                    "apikey": SUPABASE_SERVICE_ROLE_KEY,
                    "Content-Type": "application/json",
                },
                timeout=10.0,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"Could not start transcription for {dialer_call_id} ({exc}); "
            "the sweep will pick it up"
        )


def _affected_row_count(content_range: str) -> int | None:
    """Rows affected, read out of PostgREST's Content-Range header.

    The header reads "0-2/3" for a hit and "*/0" for a miss, so the count is
    whatever follows the final "/". Returns None - "can't tell" - for
    anything that isn't a plain number ("*/*", or a header some proxy
    stripped), so a caller can only ever act on a count PostgREST really
    stated, never on an assumption.
    """
    total = content_range.rsplit("/", 1)[-1].strip()
    return int(total) if total.isdigit() else None


async def update_dialer_call_recording_by_conference_sid(
    *, conference_sid: str, recording_sid: str
) -> bool | None:
    """Update recording_sid from a Conference's recordingStatusCallback -
    correlates via conference_sid (populated by dialer-conference-events),
    not parent_call_sid, since a Conference recording's own callback
    identifies the conference via ConferenceSid, not any participant's
    CallSid - a genuinely different correlation key from the old
    <Dial record> callback shape.

    Returns False when no row has this conference_sid (the caller can then
    correlate another way), True when one matched, None when unknown."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_SERVICE_ROLE_KEY not set - cannot update dialer_calls recording")
        return None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{SUPABASE_URL}{_DIALER_CALLS_URL_SUFFIX}",
                params={"conference_sid": f"eq.{conference_sid}"},
                json={"recording_sid": recording_sid},
                # count=exact is asked for HERE and nowhere else in this
                # module, deliberately. A PATCH that matches no row is a
                # perfectly healthy 204 - raise_for_status() cannot see it -
                # and this correlation key is the one that can silently miss:
                # conference_sid is written by a DIFFERENT webhook
                # (dialer-conference-events on conference-start), so if that
                # never fired the column is still NULL and this update hits
                # nothing. The symptom would be a call that looks completely
                # fine with no play button, and no error in Dograh, Twilio or
                # Supabase to explain it. The count is what makes it say so.
                headers={**_headers(), "Prefer": "count=exact"},
                timeout=5.0,
            )
            response.raise_for_status()
            affected = _affected_row_count(response.headers.get("Content-Range", ""))
            if affected == 0:
                logger.warning(
                    f"dialer_calls recording update matched 0 rows for conference "
                    f"{conference_sid} - recording {recording_sid} has no row with this "
                    "conference_sid (did dialer-conference-events miss conference-start?)"
                )
                return False
            return True if affected else None
    except Exception as exc:  # noqa: BLE001 - deliberate: this module's whole contract is "never raise"
        logger.error(f"Failed to update dialer_calls recording for conference {conference_sid}: {exc}")
        return None


async def update_dialer_call_recording_by_parent_call_sid(
    *, parent_call_sid: str, conference_sid: str, recording_sid: str
) -> bool:
    """The fallback for an orphaned conference recording: the conference is
    named after the rep's call (call-<parent_call_sid>), so once that name is
    known the recording and the missed conference_sid are written by the
    call's own key. Only fills a row that has no recording yet."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{SUPABASE_URL}{_DIALER_CALLS_URL_SUFFIX}",
                params={"parent_call_sid": f"eq.{parent_call_sid}", "recording_sid": "is.null"},
                json={"recording_sid": recording_sid, "conference_sid": conference_sid},
                headers={**_headers(), "Prefer": "count=exact"},
                timeout=5.0,
            )
            response.raise_for_status()
            return bool(_affected_row_count(response.headers.get("Content-Range", "")))
    except Exception as exc:  # noqa: BLE001 - deliberate: this module's whole contract is "never raise"
        logger.error(f"Failed to attach orphaned recording {recording_sid} to {parent_call_sid}: {exc}")
        return False
