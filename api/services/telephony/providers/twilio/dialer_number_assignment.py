"""Per-rep outbound caller ID lookup for the sales dialer.

dialer_phone_numbers lives entirely in Supabase (see
docs/superpowers/specs/2026-08-11-dialer-number-assignment-design.md in the
sysevo repo) - Dograh has no local copy of assignment data, so resolving a
rep's assigned number means a server-to-server Supabase REST call.

Unlike sysevo_roles.get_sysevo_roles, this fails OPEN, not closed: a
Supabase outage or a rep with no assignment must never block a call, only
fall back to the shared default caller ID (see routes.py's
handle_voice_connect, which tries this function first and falls back to
its own inline SYSEVO_TWILIO_DEFAULT_CALLER_ID resolution). This function
guards a display/attribution concern, not access control.
"""

import httpx
from loguru import logger

from api.constants import SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
from api.db import db_client


def _parse_rep_id_from_identity(raw_from: str) -> int | None:
    """Twilio Device-originated calls send From as "client:rep-{id}"."""
    identity = raw_from.removeprefix("client:")
    if not identity.startswith("rep-"):
        return None
    try:
        return int(identity.removeprefix("rep-"))
    except ValueError:
        return None


async def resolve_assigned_caller_id(raw_from: str) -> str | None:
    """Return the calling rep's assigned Twilio number, or None to fall
    back to the platform default (unassigned rep, unrecognized caller, or a
    Supabase/DB error). Every failure path here returns None rather than
    raising - this function must never break call setup, only degrade to
    the shared default caller ID."""
    rep_id = _parse_rep_id_from_identity(raw_from)
    if rep_id is None:
        return None

    try:
        user = await db_client.get_user_by_id(rep_id)
        if not user or not user.provider_id:
            return None

        if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
            logger.warning("SUPABASE_SERVICE_ROLE_KEY not set - cannot resolve per-rep caller id")
            return None

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/dialer_phone_numbers",
                params={
                    "select": "phone_number",
                    "assigned_user_id": f"eq.{user.provider_id}",
                    "is_active": "eq.true",
                    "limit": "1",
                },
                headers={
                    "apikey": SUPABASE_ANON_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                },
                timeout=5.0,
            )
            response.raise_for_status()
            rows = response.json()
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
        logger.error(f"Failed to resolve assigned caller id for rep {rep_id}: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 - deliberate: this function's whole contract is "never raise"
        logger.error(f"Unexpected error resolving assigned caller id for rep {rep_id}: {exc}")
        return None

    return rows[0]["phone_number"] if rows else None


async def is_manager_or_admin(provider_id: str) -> bool:
    """Checks whether a Supabase user has the sales_manager or super_admin
    role, via the service-role key. Used to gate listen-in from inside a
    Twilio-signature-authenticated webhook (no end-user bearer token is
    available there to check roles against RLS the normal way).

    Unlike resolve_assigned_caller_id, this fails CLOSED: an error here
    means NOT authorized, since this gates access to a live call's audio,
    not a display/attribution concern - the asymmetry with this module's
    other function is deliberate, not an inconsistency."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/user_roles",
                params={
                    "select": "role",
                    "user_id": f"eq.{provider_id}",
                    "role": "in.(super_admin,sales_manager)",
                },
                headers={
                    "apikey": SUPABASE_ANON_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                },
                timeout=5.0,
            )
            response.raise_for_status()
            rows = response.json()
    except Exception:  # noqa: BLE001 - deliberate: fail closed, this gates live-call audio access
        return False
    return len(rows) > 0
