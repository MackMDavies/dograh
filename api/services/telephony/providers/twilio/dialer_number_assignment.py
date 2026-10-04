"""Per-rep outbound caller ID lookup for the sales dialer.

dialer_phone_numbers lives entirely in Supabase (see
docs/superpowers/specs/2026-08-11-dialer-number-assignment-design.md in the
sysevo repo) - Dograh has no local copy of assignment data, so resolving a
rep's assigned number means a server-to-server Supabase REST call.

The active provider and caller ID are resolved from the same current rotation
slot. Missing assignments may use the provider's configured default. An
assignment lookup failure or a stale client on another provider fails closed;
it must never place a call with a caller ID from a different line/provider.
"""

import httpx
from loguru import logger

from api.constants import SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
from api.db import db_client


class DialerNumberAssignmentUnavailable(RuntimeError):
    """The rep's assigned line could not be verified reliably."""


class DialerNumberProviderMismatch(RuntimeError):
    """A stale softphone tried to place a call after rotation changed providers."""


def _parse_rep_id_from_identity(raw_from: str) -> int | None:
    """Twilio Device-originated calls send From as "client:rep-{id}".

    The "rep-" prefix is the token FORMAT, not a role claim: routes.py's
    /voice-token issues identity=f"rep-{user.id}" to everyone holding any of
    SALES_DIALER_ROLES (sales_rep, sales_closer, sales_manager, super_admin),
    so a manager's
    listen-in leg arrives with this exact shape too. That is why
    _serve_listen_in_twiml reuses this parser despite the rep-flavoured name -
    and why changing the identity format would silently break listen-in as
    well as dialling. Authorization is always a separate is_manager_or_admin
    check; this function grants nothing.
    """
    identity = raw_from.removeprefix("client:")
    if not identity.startswith("rep-"):
        return None
    try:
        return int(identity.removeprefix("rep-"))
    except ValueError:
        return None


async def resolve_assigned_caller_id(raw_from: str, provider_name: str = "twilio") -> str | None:
    """Return the active rotation line for this rep and provider."""
    rep_id = _parse_rep_id_from_identity(raw_from)
    if rep_id is None:
        return None

    try:
        selected = await resolve_assigned_dialer_number(rep_id)
        if selected:
            if selected.get("provider") != provider_name:
                raise DialerNumberProviderMismatch(
                    f"Active dialer line belongs to {selected.get('provider')}, not {provider_name}"
                )
            return selected.get("phone_number")
        return None
    except (DialerNumberProviderMismatch, DialerNumberAssignmentUnavailable):
        raise
    except Exception as exc:  # noqa: BLE001 - a lookup outage must not select another caller ID
        logger.error(f"Failed to resolve assigned caller id for rep {rep_id}: {type(exc).__name__}")
        raise DialerNumberAssignmentUnavailable("Could not verify the active caller ID") from exc


async def resolve_assigned_dialer_number(rep_id: int) -> dict | None:
    """Resolve the active number/provider assigned to a rep for SDK selection.

    The server derives the owner from its authenticated user record and reads
    the current rotation slot with the service key. A lookup outage raises so
    an assigned rep is never silently routed through a different provider.
    """
    try:
        user = await db_client.get_user_by_id(rep_id)
        if not user or not user.provider_id:
            return None
        if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
            raise DialerNumberAssignmentUnavailable("Supabase service key missing")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL.rstrip('/')}/rest/v1/rpc/dialer_current_assigned_number",
                json={"p_user_id": user.provider_id},
                headers={
                    "apikey": SUPABASE_ANON_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                },
                timeout=5.0,
            )
            response.raise_for_status()
            rows = response.json()
        return rows[0] if rows else None
    except Exception as exc:  # noqa: BLE001 - assignment is not an auth decision
        logger.error(f"Failed to resolve dialer number for rep {rep_id}: {type(exc).__name__}")
        if isinstance(exc, DialerNumberAssignmentUnavailable):
            raise
        raise DialerNumberAssignmentUnavailable("Could not read the rep's dialer assignment") from exc


async def user_owns_dialer_number(rep_id: int, phone_number: str, provider_name: str) -> bool:
    """Check any active number assigned to a rep for inbound authorization."""
    try:
        user = await db_client.get_user_by_id(rep_id)
        if not user or not user.provider_id:
            return False
        if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
            raise DialerNumberAssignmentUnavailable("Supabase service key missing")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SUPABASE_URL.rstrip('/')}/rest/v1/rpc/dialer_user_owns_number",
                json={"p_user_id": user.provider_id, "p_phone_number": phone_number, "p_provider": provider_name},
                headers={
                    "apikey": SUPABASE_ANON_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                },
                timeout=5.0,
            )
            response.raise_for_status()
            return response.json() is True
    except Exception as exc:  # noqa: BLE001 - inbound authorization fails closed
        logger.error(f"Failed to verify assigned dialer number for rep {rep_id}: {type(exc).__name__}")
        if isinstance(exc, DialerNumberAssignmentUnavailable):
            raise
        raise DialerNumberAssignmentUnavailable("Could not verify the rep's inbound dialer number") from exc


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
