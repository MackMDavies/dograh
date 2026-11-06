"""Run-scoped, signed SignalWire answer and status callbacks."""
from fastapi import APIRouter, HTTPException, Request, Response
from api.db import db_client
from api.services.telephony.factory import get_telephony_provider_for_run
from api.services.telephony.status_processor import StatusCallbackRequest, _process_status_update
from api.utils.common import get_backend_endpoints
from api.utils.telephony_helper import parse_webhook_request

router = APIRouter()


async def verified_run(request, run_id, path):
    run = await db_client.get_workflow_run_by_id(run_id)
    if not run:
        raise HTTPException(status_code=404, detail='Call not found')
    workflow = await db_client.get_workflow_by_id(run.workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail='Call not found')
    provider = await get_telephony_provider_for_run(run, workflow.organization_id)
    data, body = await parse_webhook_request(request)
    backend, _ = await get_backend_endpoints()
    expected = (run.gathered_context or {}).get('call_id')
    if (provider.PROVIDER_NAME != 'signalwire' or not expected or data.get('CallSid') != expected
        or data.get('AccountSid') != provider.project_id
        or not await provider.verify_inbound_signature(backend + path, data, dict(request.headers), body)):
        raise HTTPException(status_code=403, detail='Invalid call signature')
    return run, workflow, provider, data


@router.post('/signalwire-xml')
async def answer(request: Request, workflow_id: int, user_id: int, workflow_run_id: int, organization_id: int):
    path = '/api/v1/telephony/signalwire-xml?' + request.url.query
    run, workflow, provider, data = await verified_run(request, workflow_run_id, path)
    if workflow.id != workflow_id or workflow.organization_id != organization_id or workflow.user_id != user_id:
        raise HTTPException(status_code=403, detail='Invalid call context')
    return Response(content=await provider.get_webhook_response(workflow_id, user_id, workflow_run_id), media_type='application/xml')


@router.post('/signalwire/status/{workflow_run_id}')
async def status(request: Request, workflow_run_id: int):
    run, _, provider, data = await verified_run(request, workflow_run_id, f'/api/v1/telephony/signalwire/status/{workflow_run_id}')
    await _process_status_update(run.id, StatusCallbackRequest(**provider.parse_status_callback(data)))
    return {'status': 'ok'}
