"""HTTP controls for a call the agent is handling (Dialer Phases 2 + 3).

    POST /api/v1/telephony/dialer/handling/{call_key}/take-over    rep (own call) or manager
    POST /api/v1/telephony/dialer/handling/{call_key}/hand-to-rep  manager / admin
    POST /api/v1/telephony/dialer/handling/{call_key}/end          rep (own call) or manager

``call_key`` is the key the browser polls stages with (the Twilio parent call SID of a
single call, or the line's own call SID on a parallel line). Auth: the Supabase
session (``require_sales_dialer_role``); the account / ownership check is
``dialer_handling_authorize``. Response: ``{"state": "handed" | "ended",
"via": "session" | "direct"}``. Thin by design -- the logic is call_handling.control.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException

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
