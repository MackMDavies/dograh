"""Read approved call executions without exposing workflow configuration/logs."""
from datetime import UTC, datetime
import math

from api.db import db_client
from api.db import dialer_agent_history_client as history_db
from api.utils.transcript import generate_transcript_text


class AgentHistoryNotFound(ValueError):
    pass


def _stamp(value):
    if not isinstance(value,str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z","+00:00"))
        return stamp.astimezone(UTC).isoformat() if stamp.tzinfo else None
    except ValueError:
        return None


def _text(value):
    return value.strip() if isinstance(value,str) and value.strip() else None


async def bound_run(binding: dict):
    run_id = str(binding.get("workflow_run_id") or "")
    if not run_id.isdigit() or not binding.get("workflow_uuid"):
        raise AgentHistoryNotFound("No verified agent execution is attached to this request")
    # The run identifier comes from the service-owned dispatch record. Derive
    # its organization from the matching, snapshotted workflow before rereading.
    run = await db_client.get_workflow_run(int(run_id))
    workflow = await db_client.get_workflow_by_id(run.workflow_id) if run else None
    if not workflow or str(workflow.workflow_uuid) != binding["workflow_uuid"]:
        raise AgentHistoryNotFound("The stored execution does not match this agent call")
    run = await db_client.get_workflow_run(int(run_id),organization_id=workflow.organization_id)
    if not run:
        raise AgentHistoryNotFound("Agent execution is unavailable")
    ctx = run.initial_context or {}
    if not isinstance(ctx, dict):
        raise AgentHistoryNotFound("Agent execution identity is unavailable")
    if (ctx.get("dialer_account_id") != binding["client_account_id"]
        or ctx.get("called_number",ctx.get("phone_number")) != binding["to_number"]
        or (ctx.get("dialer_agent_request_id") is not None and ctx["dialer_agent_request_id"] != binding["id"])):
        raise AgentHistoryNotFound("Agent execution identity does not match the request")
    if binding.get("provider_call_id") and ctx.get("dialer_original_call_id") != binding["provider_call_id"]:
        raise AgentHistoryNotFound("Agent execution is attached to another provider call")
    return run


def execution_state(run, binding: dict) -> dict:
    logs = run.logs or {}
    callbacks = logs.get("telephony_status_callbacks",[]) if isinstance(logs,dict) else []
    if not isinstance(callbacks, list):
        callbacks = []
    connected = _stamp(binding.get("connected_at"))
    ended = _stamp(binding.get("ended_at"))
    outcome = _text(binding.get("provider_outcome"))
    duration = None
    for callback in callbacks:
        if not isinstance(callback,dict):
            continue
        status = str(callback.get("status") or "").lower()
        stamp = _stamp(callback.get("timestamp"))
        if status in {"answered","in-progress"} and stamp:
            connected = min(connected,stamp) if connected else stamp
        if status in {"completed","failed","busy","no-answer","canceled","error"}:
            outcome = status
            if stamp:
                ended = min(ended,stamp) if ended else stamp
            raw_duration = callback.get("duration")
            if isinstance(raw_duration,(str,int,float)) and not isinstance(raw_duration,bool):
                try:
                    number = float(raw_duration)
                    if math.isfinite(number) and number>=0:
                        duration = int(number)
                except ValueError:
                    pass
    failed = outcome in {"failed","busy","no-answer","canceled","error"}
    status = "failed" if failed else "completed" if run.is_completed or outcome == "completed" else "live" if run.state=="running" else None
    if binding.get("status") in {"completed","failed"}:
        status = binding["status"]
    return {"status":status,"connected_at":connected,"ended_at":ended,"provider_outcome":outcome,"duration_seconds":duration}


async def sync_binding(binding: dict, run=None) -> dict:
    run = run or await bound_run(binding)
    state = execution_state(run,binding)
    applied = await history_db.rpc("dialer_apply_agent_history_state",{
        "p_call_id":binding["id"],"p_run_id":str(run.id),"p_status":state["status"],
        "p_connected_at":state["connected_at"],"p_ended_at":state["ended_at"],"p_provider_outcome":state["provider_outcome"],
    })
    if applied is not True:
        raise history_db.HistoryStorageUnavailable("Agent execution binding changed during reconciliation")
    return state


async def authorize_call(call_id: str, authorization: str) -> dict:
    if await history_db.rpc("dialer_can_read_agent_call",{"p_call_id":call_id},authorization=authorization) is not True:
        raise AgentHistoryNotFound("Agent call not found")
    binding = await history_db.rpc("dialer_agent_history_binding",{"p_call_id":call_id})
    if not isinstance(binding,dict):
        raise AgentHistoryNotFound("Agent call not found")
    return binding


async def details(call_id: str, authorization: str) -> dict:
    binding = await authorize_call(call_id,authorization)
    run = await bound_run(binding)
    state = await sync_binding(binding,run)
    analysis = binding.get("analysis") or {}
    analysis = analysis if isinstance(analysis, dict) else {}
    gathered = run.gathered_context or {}
    gathered = gathered if isinstance(gathered, dict) else {}
    logs = run.logs or {}
    events = logs.get("realtime_feedback_events",[]) if isinstance(logs,dict) else []
    events = [event for event in events if isinstance(event, dict) and isinstance(event.get("payload"), dict)] if isinstance(events, list) else []
    facts = {key:gathered[key] for key in ("role","pain","tried","value","next_step","current_tool","decision_makers")
        if isinstance(gathered.get(key),(str,int,float,bool))}
    return {
        "id":binding["id"],"agent_name":binding.get("agent_name"),"to_number":binding["to_number"],
        "role":binding["role"],"status":state["status"] or binding["status"],"created_at":binding["created_at"],
        "connected_at":state["connected_at"],"ended_at":state["ended_at"],"duration_seconds":state["duration_seconds"],
        "provider_outcome":state["provider_outcome"],
        "summary":_text(analysis.get("summary")) or _text(gathered.get("call_summary")) or _text(gathered.get("summary")),
        "outcome":_text(analysis.get("outcome")) or _text(gathered.get("mapped_call_disposition")),
        "sentiment":_text(analysis.get("sentiment")),"analysis_status":analysis.get("status"),
        "transcript":generate_transcript_text(events),"captured":facts,
        "has_recording":run.recording_url==f"recordings/{run.id}.wav",
    }


async def recording(call_id: str, authorization: str) -> bytes:
    binding = await authorize_call(call_id,authorization)
    run = await bound_run(binding)
    # Paths are server-owned and must belong to this exact run. No public
    # artifact token, storage credential, arbitrary path or URL is exposed.
    if run.recording_url != f"recordings/{run.id}.wav":
        raise AgentHistoryNotFound("This call's recording is not available")
    from api.services.storage import get_storage_for_backend
    data = await get_storage_for_backend(run.storage_backend).aread_bytes(run.recording_url)
    if data is None:
        raise AgentHistoryNotFound("This call's recording is not available")
    return data
