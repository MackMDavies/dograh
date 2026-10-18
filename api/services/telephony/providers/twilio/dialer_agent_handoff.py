"""Adopt an answered parallel prospect into the configured agent runtime.

This path never originates a second PSTN call. The stored agent is resolved
from a service-only, account-scoped reservation; transport credentials must
belong to its workflow organization AND the account owning the existing call.
"""

import asyncio
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from xml.sax.saxutils import quoteattr

import httpx
from fastapi import WebSocket
from loguru import logger

from api.constants import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
from api.db import db_client
from api.services.quota_service import check_dograh_quota_by_user_id
from api.services.telephony.call_concurrency import (
    acquire_call_slot, bind_slot, release_call_slot, release_slot_for_failed_start,
)
from api.services.telephony.factory import (
    find_telephony_config_for_inbound, get_telephony_provider_by_id,
    get_telephony_provider_for_run,
)
from api.services.workflow.variable_resolution import sanitize_context_variables
from api.utils.common import get_backend_endpoints


class AgentHandoffUnavailable(RuntimeError):
    """The answered call cannot safely be adopted by its configured agent."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class PreparedAgentHandoff:
    workflow_run_id: int
    twiml: str
    already_streaming: bool = False


async def _run_agent_pipeline(websocket, **kwargs):
    """Use the standard runtime; keep provider orchestration independently testable."""
    from api.services.pipecat.run_pipeline import run_pipeline_telephony
    await run_pipeline_telephony(websocket, **kwargs)


async def _rpc(name: str, payload: dict):
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise AgentHandoffUnavailable("Agent handoff storage is unavailable",retryable=True)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(f"{SUPABASE_URL}/rest/v1/rpc/{name}", json=payload, headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            })
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        raise AgentHandoffUnavailable("Could not persist agent handoff state",retryable=True) from exc


async def record_handoff_event(
    *, call_id: str, event: str, event_at: str,
    workflow_run_id: str | None = None, duration_seconds: int | None = None,
) -> bool:
    if event == "started":
        try:
            run = await db_client.get_workflow_run(int(workflow_run_id or ""))
        except (TypeError,ValueError):
            return False
        if not run or (run.initial_context or {}).get("dialer_original_call_id") != call_id:
            return False
        if (run.gathered_context or {}).get("dialer_media_authenticated") is not True:
            raise AgentHandoffUnavailable("The call's media has not been authenticated yet",retryable=True)
    recorded = await _rpc("dialer_record_twilio_agent_handoff", {
        "p_child_call_sid": call_id, "p_event": event, "p_event_at": event_at,
        "p_workflow_run_id": workflow_run_id, "p_duration_seconds": duration_seconds,
    }) is True
    if recorded and event == "ended":
        run = await db_client.get_workflow_run_by_call_id(call_id)
        context = (run.initial_context or {}) if run else {}
        if run and run.state == "initialized" and context.get("dialer_original_call_id") == call_id:
            workflow = await db_client.get_workflow_by_id(run.workflow_id)
            if workflow and await db_client.finish_unstarted_dialer_agent_run(run.id,workflow.organization_id,call_id):
                await release_call_slot(run.id)
    return recorded


def media_token(*, auth_token: str, run_id: int, call_id: str) -> str:
    """A token for one stored run and existing call; credentials stay server-side."""
    return hmac.new(auth_token.encode(), f"dialer-agent-media:{run_id}:{call_id}".encode(), hashlib.sha256).hexdigest()


async def _provider_for_existing_call(workflow, plan: dict, account_sid: str):
    config_id = plan.get("telephony_configuration_id")
    if config_id is None:
        match = await find_telephony_config_for_inbound(workflow.organization_id, "twilio", account_sid)
        if not match:
            raise AgentHandoffUnavailable("The agent has no configuration for this call's Twilio account")
        config_id = match[0]
    row = await db_client.get_telephony_configuration_for_org(int(config_id), workflow.organization_id)
    if not row or row.provider != "twilio" or (row.credentials or {}).get("account_sid") != account_sid:
        raise AgentHandoffUnavailable("The agent configuration does not own this call")
    provider = await get_telephony_provider_by_id(row.id, workflow.organization_id)
    if provider.PROVIDER_NAME != "twilio" or provider.account_sid != account_sid or not provider.auth_token:
        raise AgentHandoffUnavailable("The agent transport is unavailable for this call")
    return provider, row.id


async def agent_handoff_capability(*, rep_user_id: str, account_sid: str) -> dict:
    """Configuration preflight; never reserves a run, quota slot or phone call."""
    if not account_sid:
        return {"available":False,"reason":"A compatible Twilio phone connection is required."}
    try:
        plan = await _rpc("dialer_parallel_agent_configuration",{"p_rep_user_id":rep_user_id})
    except AgentHandoffUnavailable:
        return {"available":False,"reason":"Agent availability could not be verified. Try again shortly."}
    if not isinstance(plan,dict):
        return {"available":False,"reason":"Assign an active outbound agent in Agents settings."}
    workflow = await db_client.get_workflow_by_uuid_unscoped(plan["workflow_uuid"])
    if not workflow or workflow.status != "active" or not workflow.released_definition or not workflow.user_id:
        return {"available":False,"reason":"Publish and activate the assigned outbound agent."}
    try:
        await _provider_for_existing_call(workflow,plan,account_sid)
        return {"available":True,"reason":None}
    except (AgentHandoffUnavailable,ValueError):
        return {"available":False,"reason":"Connect the outbound agent to the same Twilio account as the dialer."}


async def prepare_agent_handoff(
    *, parent_call_sid: str, child_call_sid: str, leg_id: str,
    account_sid: str, allow_fallback: bool = False,
) -> PreparedAgentHandoff | None:
    if not account_sid:
        raise AgentHandoffUnavailable("The provider account is missing")
    plan = await _rpc("dialer_prepare_twilio_agent_handoff", {
        "p_parent_call_sid": parent_call_sid, "p_child_call_sid": child_call_sid,
        "p_leg_id": leg_id, "p_allow_fallback": allow_fallback,
    })
    if not isinstance(plan, dict):
        return None
    if plan.get("status") in {"completed", "failed"}:
        return None
    if plan.get("call_id") != child_call_sid or str(plan.get("request_id")) != leg_id:
        raise AgentHandoffUnavailable("Agent handoff identity does not match the reserved line")
    # This UUID comes from the approved server-side catalogue, never from the
    # browser or provider query string. Derive the execution organization here.
    workflow = await db_client.get_workflow_by_uuid_unscoped(plan["workflow_uuid"])
    if not workflow or workflow.status != "active" or not workflow.user_id or not workflow.released_definition:
        raise AgentHandoffUnavailable("The assigned agent is not published and active")
    provider, config_id = await _provider_for_existing_call(workflow, plan, account_sid)
    run = await db_client.get_workflow_run_by_call_id(child_call_sid)
    if run:
        context = run.initial_context or {}
        if (run.workflow_id != workflow.id or context.get("dialer_agent_request_id") != str(plan["id"])
            or context.get("dialer_original_call_id") != child_call_sid):
            raise AgentHandoffUnavailable("This call is already attached to a different execution")
        if context.get("telephony_configuration_id") != config_id or run.is_completed:
            raise AgentHandoffUnavailable("The stored handoff is no longer available")
        if run.state == "running":
            return PreparedAgentHandoff(run.id,"",already_streaming=True)
    else:
        quota = await check_dograh_quota_by_user_id(workflow.user_id, workflow_id=workflow.id)
        if not quota.has_quota:
            raise AgentHandoffUnavailable("The agent has no available call quota")
        allowed, slot = await acquire_call_slot(workflow.organization_id)
        if not allowed:
            raise AgentHandoffUnavailable("The agent's concurrent call limit has been reached")
        defaults = sanitize_context_variables(
            getattr(workflow.released_definition, "template_context_variables", None)
            or getattr(workflow, "template_context_variables", None)
        )
        try:
            run = await db_client.create_workflow_run(
                name=f"WR-DIALER-{plan['id']}", workflow_id=workflow.id, mode="twilio",
                user_id=workflow.user_id, organization_id=workflow.organization_id, use_draft=False,
                idempotency_key=f"dialer-agent:{plan['id']}",
                initial_context={
                    **defaults, "provider": "twilio", "direction": "outbound",
                    "phone_number": plan["to_number"], "called_number": plan["to_number"],
                    "caller_number": plan["from_number"], "telephony_configuration_id": config_id,
                    "dialer_agent_request_id": str(plan["id"]), "dialer_original_call_id": child_call_sid,
                    "dialer_account_id": plan["account_id"], "dialer_entry_id": plan["entry_id"],
                    "dialer_rep_user_id": plan["rep_user_id"], "dialer_parent_call_sid": parent_call_sid,
                    "dialer_concurrency_slot": slot,
                },
                gathered_context={"call_id": child_call_sid, "provider": "twilio"},
            )
        except Exception:
            await release_slot_for_failed_start(workflow.organization_id, slot)
            raise
        # A duplicate webhook may have acquired a slot before the first worker
        # committed its run. Postgres returns that run; return the redundant slot.
        if (run.initial_context or {}).get("dialer_concurrency_slot") != slot:
            await release_slot_for_failed_start(workflow.organization_id, slot)
        else:
            await bind_slot(run.id, workflow.organization_id, slot)
    recorded = await record_handoff_event(
        call_id=child_call_sid, event="prepared", event_at=datetime.now(UTC).isoformat(),
        workflow_run_id=str(run.id),
    )
    if not recorded:
        raise AgentHandoffUnavailable("Could not attach the workflow execution to this call",retryable=True)
    backend_endpoint, websocket_endpoint = await get_backend_endpoints()
    if not str(backend_endpoint).startswith("https://") or not str(websocket_endpoint).startswith("wss://"):
        raise AgentHandoffUnavailable("Secure agent media endpoints are unavailable")
    stream_url = f"{str(websocket_endpoint).rstrip('/')}/api/v1/telephony/dialer-agent-media/{run.id}"
    status_url = f"{str(backend_endpoint).rstrip('/')}/api/v1/telephony/dialer-agent-stream-status?workflow_run_id={run.id}"
    token = media_token(auth_token=provider.auth_token, run_id=run.id, call_id=child_call_sid)
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        '<Say>This call may be recorded and monitored for quality assurance.</Say>'
        f'<Connect><Stream url={quoteattr(stream_url)} statusCallback={quoteattr(status_url)}>'
        f'<Parameter name="handoffToken" value={quoteattr(token)}/>'
        '</Stream></Connect><Hangup/></Response>'
    )
    return PreparedAgentHandoff(run.id, twiml)


async def serve_agent_media(websocket: WebSocket, workflow_run_id: int) -> None:
    """Authenticate the provider start frame before claiming or running any audio."""
    claimed = False
    try:
        run = await db_client.get_workflow_run(workflow_run_id)
        context = (run.initial_context or {}) if run else {}
        if not run or run.state != "initialized" or not context.get("dialer_agent_request_id"):
            await websocket.close(code=4409, reason="Agent handoff is not available")
            return
        workflow = await db_client.get_workflow_by_id(run.workflow_id)
        if not workflow or not workflow.user_id:
            await websocket.close(code=4404, reason="Agent unavailable")
            return
        config_id = context.get("telephony_configuration_id")
        config = await db_client.get_telephony_configuration_for_org(config_id, workflow.organization_id)
        if not config or config.provider != "twilio":
            await websocket.close(code=4403, reason="Invalid handoff configuration")
            return
        provider = await get_telephony_provider_for_run(run, workflow.organization_id)
        first = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        start = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        details = start.get("start") or {}
        call_id = str(details.get("callSid") or "")
        custom = details.get("customParameters") or {}
        token = str(custom.get("handoffToken") or "")
        expected_call_id = str(context.get("dialer_original_call_id") or "")
        media_format = details.get("mediaFormat") or {}
        if (
            provider.PROVIDER_NAME != "twilio" or not provider.auth_token
            or first.get("event") != "connected" or start.get("event") != "start"
            or not details.get("streamSid") or call_id != expected_call_id
            or details.get("accountSid") != provider.account_sid
            or media_format != {"encoding":"audio/x-mulaw","sampleRate":8000,"channels":1}
            or not hmac.compare_digest(token, media_token(auth_token=provider.auth_token, run_id=run.id, call_id=call_id))
        ):
            await websocket.close(code=4403, reason="Invalid call-scoped media credential")
            return
        claimed = await db_client.claim_dialer_agent_media(run.id, workflow.organization_id, call_id)
        if not claimed:
            await websocket.close(code=4409, reason="Agent media was already claimed")
            return
        # The provider status callback can arrive before this socket is
        # authenticated and is not guaranteed to be retried. Persist the actual
        # accepted-media event here as well; a later signed provider timestamp
        # can refine it through the same idempotent database transition.
        if not await record_handoff_event(
            call_id=call_id, event="started", event_at=datetime.now(UTC).isoformat(),
            workflow_run_id=str(run.id),
        ):
            raise AgentHandoffUnavailable("Could not record the authenticated agent media")
        from pipecat.utils.run_context import set_current_run_id

        set_current_run_id(run.id)
        await _run_agent_pipeline(
            websocket, provider_name="twilio", workflow_id=workflow.id,
            workflow_run_id=run.id, user_id=workflow.user_id, call_id=call_id,
            transport_kwargs={"stream_sid": details["streamSid"], "call_sid": call_id},
        )
    except Exception as exc:
        logger.error(f"Agent handoff media failed for run {workflow_run_id}: {type(exc).__name__}")
        if claimed:
            await db_client.update_workflow_run(workflow_run_id, is_completed=True, state="completed")
        try:
            await websocket.close(code=1011, reason="Agent media unavailable")
        except RuntimeError:
            pass
    finally:
        if claimed:
            await release_call_slot(workflow_run_id)
