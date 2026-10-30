"""Developers console reads: request logs and usage figures for the caller's organisation.

Both routes are org-scoped through ``get_user`` (session JWT or API key) and only read
rows written by ``api.services.api_request_logging``.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.db import db_client
from api.db.models import UserModel
from api.services.api_request_logging import RETENTION_DAYS
from api.services.auth.depends import get_user

router = APIRouter(prefix="/organizations/developer", tags=["developer"])


def _org(user: UserModel) -> int:
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")
    return user.selected_organization_id


@router.get("/request-logs")
async def list_request_logs(
    surface: Optional[str] = Query(None, description="rest, public or mcp"),
    errors: bool = Query(False, description="Only responses with status >= 400"),
    after_id: Optional[int] = Query(None, description="Only rows newer than this id (live tail)"),
    limit: int = Query(60, ge=1, le=200),
    user: UserModel = Depends(get_user),
):
    rows = await db_client.list_api_request_logs(
        _org(user), surface=surface, errors_only=errors, after_id=after_id, limit=limit
    )
    logs = [
        {
            "id": r["id"],
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            "surface": r["surface"],
            "method": r["method"],
            "path": r["path"],
            "mcp_method": r["mcp_method"],
            "status": r["status"],
            "latency_ms": r["latency_ms"],
            "user_agent": r["user_agent"],
            "api_key_id": r["api_key_id"],
            "key_name": r["key_name"],
            "key_prefix": r["key_prefix"],
        }
        for r in rows
    ]
    return {"logs": logs, "retention_days": RETENTION_DAYS}


@router.get("/request-stats")
async def request_stats(
    days: int = Query(30, ge=1, le=90),
    user: UserModel = Depends(get_user),
):
    stats = await db_client.api_request_stats(_org(user), days=days)
    return {**stats, "retention_days": RETENTION_DAYS}
