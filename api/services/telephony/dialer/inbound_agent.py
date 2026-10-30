"""Reading which agent takes inbound calls nobody answers. The rules live in
inbound_agent_rules.py; this is the I/O.

Two reads: the 'inbound' row of dialer_agent_assignments (Supabase, where the dialer's
Agents page saves it) and, for the agent picked there, its workflow and assigned numbers
(Dograh's own database). Cached briefly: this sits on the path of every inbound call, and a
change on the page reaching callers within half a minute is soon enough.
"""

from __future__ import annotations

import os
import time

import httpx
from loguru import logger

from api.constants import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
from api.db import db_client
from api.db.dialer_agent_routing_client import inbound_scope
from api.services.telephony.dialer.inbound_agent_rules import (
    InboundAgent,
    choose_inbound_agent,
    hands_to_agent,
)

_TTL_SECONDS = 30.0
_cache: dict[str, tuple[float, InboundAgent]] = {}


async def _supabase_get(table: str, params: dict) -> list[dict] | None:
    """One service-role read. None when it could not be read -- never confused with "no rows"."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/{table}",
                params=params,
                headers={"apikey": SUPABASE_SERVICE_ROLE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"},
                timeout=3.0,
            )
            response.raise_for_status()
            rows = response.json()
            return rows if isinstance(rows, list) else None
    except Exception as exc:  # noqa: BLE001 - a live caller is waiting on this path
        logger.warning(f"inbound-agent: reading {table} failed: {exc}")
        return None


async def _resolve(scope: dict) -> InboundAgent:
    assignment = scope.get("assignment")
    workflow_id: int | None = None
    numbers: list[str] = []
    picked = (assignment or {}).get("demo_agent")
    if picked and picked.get("workflow_uuid"):
        try:
            workflow = await db_client.get_workflow_by_uuid_unscoped(picked["workflow_uuid"])
            if workflow is not None:
                workflow_id = workflow.id
                phones = await db_client.list_phone_numbers_for_workflows([workflow.id])
                numbers = [p.address for p in sorted(phones, key=lambda p: p.id, reverse=True) if p.is_active]
        except Exception as exc:  # noqa: BLE001
            logger.error(f"inbound-agent: resolving the chosen agent failed: {exc}")
    return choose_inbound_agent(
        assignment=assignment,
        workflow_id=workflow_id,
        numbers=numbers,
        env_number=os.environ.get("SYSEVO_SAM_INBOUND_NUMBER") if scope.get("platform") else None,
        env_workflow_id=(os.environ.get("SYSEVO_SAM_INBOUND_WORKFLOW_ID") or "214") if scope.get("platform") else None,
        env_ring_first=os.environ.get("SYSEVO_SAM_RING_FIRST_SECONDS") if scope.get("platform") else None,
    )


async def current_inbound_agent(*, fresh: bool = False, number: str | None = None,
    user_id: str | None = None, call_id: str | None = None) -> InboundAgent:
    """The agent callers are handed to right now. `fresh` skips the cache (the status check)."""
    try:
        scope = await inbound_scope(number=number,user_id=user_id,call_id=call_id)
    except Exception:
        scope = None
    if not scope or not scope.get("client_account_id"):
        return InboundAgent(None,"",0,"off","The inbound account could not be verified.")
    account = str(scope["client_account_id"])
    now = time.monotonic()
    cached = _cache.get(account)
    if not fresh and cached and now - cached[0] < _TTL_SECONDS:
        return cached[1]
    agent = await _resolve(scope)
    _cache[account] = (now, agent)
    return agent


async def number_hands_to_agent(number: str) -> bool:
    """Whether this rep number was left switched on for hand-over (the default)."""
    if not number:
        return True
    rows = await _supabase_get(
        "dialer_phone_numbers", {"phone_number": f"eq.{number}", "select": "hands_to_agent"}
    )
    return hands_to_agent(rows or [])
