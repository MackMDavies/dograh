"""Agent history access is authorized through the signed-in Sysevo account."""
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse, Response

from api.services.auth.depends import get_user
from api.services.telephony.dialer import agent_history
from api.db.dialer_agent_history_client import HistoryStorageUnavailable

router = APIRouter(prefix="/dialer/agent-calls",dependencies=[Depends(get_user)])


async def _authorized(action, call_id: UUID, authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401,detail="Sign in to view this call")
    try:
        return await action(str(call_id),authorization)
    except agent_history.AgentHistoryNotFound as exc:
        raise HTTPException(status_code=404,detail=str(exc))
    except HistoryStorageUnavailable:
        raise HTTPException(status_code=503,detail="Agent call history is temporarily unavailable")


@router.get("/{call_id}")
async def get_agent_history(call_id: UUID,authorization: str | None = Header(None)):
    data = await _authorized(agent_history.details,call_id,authorization)
    return JSONResponse(content=data,headers={"Cache-Control":"private, no-store"})


@router.get("/{call_id}/recording")
async def get_agent_recording(call_id: UUID,authorization: str | None = Header(None)):
    data = await _authorized(agent_history.recording,call_id,authorization)
    return Response(content=data,media_type="audio/wav",headers={"Cache-Control":"private, no-store"})
