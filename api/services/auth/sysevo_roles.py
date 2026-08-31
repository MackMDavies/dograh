"""Sysevo-side role lookups for gating internal-staff-only features.

Dograh's own UserModel has no concept of Sysevo roles (sales_rep,
sales_manager, client, ...) — that lives entirely in Supabase's
user_roles table. This queries it over Supabase's REST API using the
anon key plus the caller's own bearer token, which user_roles' RLS
policy ("Users can view all roles", USING (true)) already permits for
any authenticated user — no service-role key or new edge function needed.
"""

import httpx
from fastapi import Depends, Header, HTTPException
from loguru import logger

from api.constants import AUTH_PROVIDER, SUPABASE_ANON_KEY, SUPABASE_URL
from api.db.models import UserModel
from api.services.auth.depends import get_user

SALES_DIALER_ROLES = {"sales_rep", "sales_closer", "sales_manager", "super_admin"}

_ACCESS_DENIED_DETAIL = "This feature requires a Sysevo sales role."


async def get_sysevo_roles(supabase_user_id: str, access_token: str) -> list[str]:
    """Look up a user's Sysevo roles via Supabase's user_roles REST endpoint.

    `supabase_user_id` must be server-verified (e.g. UserModel.provider_id,
    itself derived from a validated Supabase JWT) — user_roles' RLS policy
    ("Users can view all roles", USING (true)) lets any authenticated caller
    read ANY user's roles, so passing a client-supplied id here would turn
    this into a cross-user role-disclosure primitive.
    """
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise HTTPException(
            status_code=503,
            detail="SUPABASE_URL and SUPABASE_ANON_KEY must be set to resolve Sysevo roles.",
        )

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/user_roles",
                params={"select": "role", "user_id": f"eq.{supabase_user_id}"},
                headers={
                    "apikey": SUPABASE_ANON_KEY,
                    "Authorization": f"Bearer {access_token}",
                },
                timeout=10.0,
            )
            response.raise_for_status()
            rows = response.json()
    except httpx.HTTPError as exc:
        # Fail closed: a Supabase outage/timeout must deny access, never be
        # mistaken for "checked, no roles found".
        logger.error(f"Failed to resolve Sysevo roles for user {supabase_user_id}: {exc}")
        raise HTTPException(status_code=503, detail="Unable to verify Sysevo role.") from exc

    return [row["role"] for row in rows]


async def require_sales_dialer_role(
    authorization: str | None = Header(None),
    user: UserModel = Depends(get_user),
) -> UserModel:
    if user.is_superuser:
        return user

    if AUTH_PROVIDER != "supabase" or not authorization:
        # Local/OSS/Stack auth have no Sysevo role system to check against;
        # fail closed rather than silently allow access this dependency
        # exists specifically to restrict.
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED_DETAIL)

    token = authorization.removeprefix("Bearer ").strip()
    roles = await get_sysevo_roles(user.provider_id, token)

    if SALES_DIALER_ROLES.isdisjoint(roles):
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED_DETAIL)

    return user
