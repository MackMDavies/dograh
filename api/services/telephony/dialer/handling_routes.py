"""HTTP controls for a call the agent is handling (Dialer Phases 2 + 3).

    POST /api/v1/telephony/dialer/handling/{call_key}/take-over    rep (own call) or manager
    POST /api/v1/telephony/dialer/handling/{call_key}/hand-to-rep  manager / admin
    POST /api/v1/telephony/dialer/handling/{call_key}/end          rep (own call) or manager
    POST /api/v1/telephony/dialer/calling-hours                    any dialer user (the
         server's calling-hours decision for the browser's two-step warning)

``call_key`` is the key the browser polls stages with (the Twilio parent call SID of a
single call, or the line's own call SID on a parallel line). Auth: the Supabase
session (``require_sales_dialer_role``); the account / ownership check is
``dialer_handling_authorize``. Response: ``{"state": "handed" | "ended",
"via": "session" | "direct"}``. Thin by design -- the logic is call_handling.control.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.services.auth.sysevo_roles import require_sales_dialer_role
from api.services.telephony.dialer import call_handling

router = APIRouter()
_KEY = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")


async def _run(call_key: str, action: str, user) -> dict:
    if not _KEY.fullmatch(call_key or ""):
        raise HTTPException(status_code=400, detail="Invalid call id.")
    user_id = getattr(user, "provider_id", None)
    if not isinstance(user_id, str) or not user_id:
        raise HTTPException(status_code=503, detail="Your account is missing its dialer user mapping.")
    try:
        return await call_handling.control(call_key, action, user_id)
    except call_handling.HandlingControlError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc


@router.post("/dialer/handling/{call_key}/take-over")
async def take_over_handled_call(call_key: str, user=Depends(require_sales_dialer_role)):
    """The agent leaves first, then the rep is connected (< 1 s)."""
    return await _run(call_key, "take_over", user)


@router.post("/dialer/handling/{call_key}/hand-to-rep")
async def hand_handled_call_to_rep(call_key: str, user=Depends(require_sales_dialer_role)):
    return await _run(call_key, "hand_to_rep", user)


@router.post("/dialer/handling/{call_key}/end")
async def end_handled_call(call_key: str, user=Depends(require_sales_dialer_role)):
    return await _run(call_key, "end", user)


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class CallingHoursRequest(BaseModel):
    phone: str
    entry_id: str | None = None


@router.post("/dialer/calling-hours")
async def dialer_calling_hours(body: CallingHoursRequest, user=Depends(require_sales_dialer_role)):
    """The SAME calling-hours decision the call webhooks use, for the browser's warning.

    The browser asks before every outbound dial, so a call the server considers out of
    hours always shows the two-step warning first and is never refused without it.
    POST so the prospect's number never appears in a URL or an access log.
    """
    from api.services.telephony.dialer.compliance import calling_hours_detail
    from api.services.telephony.dialer.signalwire_routes import normalize_lead_number

    del user
    normalized = normalize_lead_number(body.phone or "")
    if not normalized:
        raise HTTPException(status_code=400, detail="Not a dialable number.")
    entry = body.entry_id if body.entry_id and _UUID.fullmatch(body.entry_id) else None
    return await calling_hours_detail(normalized, entry)
