"""Real handoff orchestration with storage/provider boundaries mocked; no calls placed."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from xml.etree import ElementTree

import pytest

from api.services.telephony.providers.twilio import dialer_agent_handoff as handoff

MOD = "api.services.telephony.providers.twilio.dialer_agent_handoff"
CALL = "CA" + "1" * 32
PARENT = "CA" + "2" * 32
ACCOUNT = "AC" + "3" * 32
LEG = "11111111-1111-4111-8111-111111111111"
REQUEST = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def deps():
    plan = {
        "id": REQUEST,"request_id": LEG,"account_id": "business-account",
        "rep_user_id": "rep-uuid","entry_id": "entry-uuid","workflow_uuid": "approved-workflow",
        "telephony_configuration_id": 77,"workflow_run_id": None,"status": "requested",
        "call_id": CALL,"from_number": "+14155550100","to_number": "+14155550123",
    }
    workflow = SimpleNamespace(
        id=33,user_id=99,organization_id=11,status="active",
        released_definition=SimpleNamespace(template_context_variables={"business": "Test company"}),
    )
    provider = SimpleNamespace(PROVIDER_NAME="twilio",account_sid=ACCOUNT,auth_token="private-token",initiate_call=AsyncMock())
    db = MagicMock()
    db.get_workflow_by_uuid_unscoped = AsyncMock(return_value=workflow)
    db.get_telephony_configuration_for_org = AsyncMock(return_value=SimpleNamespace(id=77,provider="twilio",credentials={"account_sid": ACCOUNT}))
    db.get_workflow_run_by_call_id = AsyncMock(return_value=None)
    async def create(**kwargs):
        return SimpleNamespace(id=501,workflow_id=33,initial_context=kwargs["initial_context"],is_completed=False,state="initialized")
    db.create_workflow_run = AsyncMock(side_effect=create)
    rpc = AsyncMock(side_effect=lambda name, body: plan if name in {"dialer_prepare_twilio_agent_handoff","dialer_parallel_agent_configuration"} else True)
    with (
        patch(f"{MOD}.db_client",db),patch(f"{MOD}._rpc",rpc),
        patch(f"{MOD}.get_telephony_provider_by_id",new=AsyncMock(return_value=provider)),
        patch(f"{MOD}.check_dograh_quota_by_user_id",new=AsyncMock(return_value=SimpleNamespace(has_quota=True))) as quota,
        patch(f"{MOD}.acquire_call_slot",new=AsyncMock(return_value=(True,"slot-a"))) as acquire,
        patch(f"{MOD}.bind_slot",new=AsyncMock()) as bind,
        patch(f"{MOD}.release_slot_for_failed_start",new=AsyncMock()) as release,
        patch(f"{MOD}.get_backend_endpoints",new=AsyncMock(return_value=("https://api.example.com","wss://api.example.com"))),
    ):
        yield SimpleNamespace(plan=plan,workflow=workflow,provider=provider,db=db,rpc=rpc,quota=quota,acquire=acquire,bind=bind,release=release)


async def prepare():
    return await handoff.prepare_agent_handoff(parent_call_sid=PARENT,child_call_sid=CALL,leg_id=LEG,account_sid=ACCOUNT)


async def test_adopts_existing_call_with_published_owner_and_exact_provider_config(deps):
    result = await prepare()
    root = ElementTree.fromstring(result.twiml)
    assert root.find("Dial") is None
    stream = root.find("Connect/Stream")
    assert stream is not None and stream.attrib["url"].endswith("/dialer-agent-media/501")
    token = stream.find("Parameter").attrib["value"]
    assert token == handoff.media_token(auth_token="private-token",run_id=501,call_id=CALL)
    assert "private-token" not in result.twiml
    args = deps.db.create_workflow_run.await_args.kwargs
    assert args["organization_id"] == 11 and args["user_id"] == 99
    assert args["use_draft"] is False
    assert args["idempotency_key"] == f"dialer-agent:{REQUEST}"
    assert args["gathered_context"]["call_id"] == CALL
    assert args["initial_context"]["telephony_configuration_id"] == 77
    assert args["initial_context"]["business"] == "Test company"
    deps.provider.initiate_call.assert_not_awaited()
    deps.bind.assert_awaited_once_with(501,11,"slot-a")


async def test_replayed_handoff_reuses_execution_and_does_not_acquire_another_slot(deps):
    deps.db.get_workflow_run_by_call_id.return_value = SimpleNamespace(
        id=501,workflow_id=33,is_completed=False,state="initialized",
        initial_context={"dialer_agent_request_id": REQUEST,"telephony_configuration_id": 77,"dialer_original_call_id":CALL},
    )
    result = await prepare()
    assert result.workflow_run_id == 501
    deps.db.create_workflow_run.assert_not_awaited()
    deps.acquire.assert_not_awaited()
    deps.provider.initiate_call.assert_not_awaited()


async def test_recovery_of_running_agent_never_restarts_its_media(deps):
    deps.db.get_workflow_run_by_call_id.return_value = SimpleNamespace(
        id=501,workflow_id=33,is_completed=False,state="running",
        initial_context={"dialer_agent_request_id":REQUEST,"telephony_configuration_id":77,"dialer_original_call_id":CALL},
    )
    result = await prepare()
    assert result.already_streaming is True and result.twiml == ""
    deps.acquire.assert_not_awaited()
    deps.db.create_workflow_run.assert_not_awaited()


@pytest.mark.parametrize("row", [None,SimpleNamespace(id=77,provider="telnyx",credentials={}),SimpleNamespace(id=77,provider="twilio",credentials={"account_sid": "another-account"})])
async def test_rejects_missing_cross_org_or_wrong_provider_config(deps,row):
    deps.db.get_telephony_configuration_for_org.return_value = row
    with pytest.raises(handoff.AgentHandoffUnavailable):
        await prepare()
    deps.db.create_workflow_run.assert_not_awaited()
    deps.provider.initiate_call.assert_not_awaited()


@pytest.mark.parametrize("field,value", [("call_id","other-call"),("request_id","other-leg")])
async def test_rejects_a_plan_for_another_line(deps,field,value):
    deps.plan[field] = value
    with pytest.raises(handoff.AgentHandoffUnavailable):
        await prepare()
    deps.db.get_workflow_by_uuid_unscoped.assert_not_awaited()


async def test_no_agent_reservation_means_no_runtime_or_provider_action(deps):
    deps.rpc.return_value = None
    deps.rpc.side_effect = None
    assert await prepare() is None
    deps.db.get_workflow_by_uuid_unscoped.assert_not_awaited()


async def test_inactive_or_unpublished_agent_does_not_run(deps):
    deps.workflow.released_definition = None
    with pytest.raises(handoff.AgentHandoffUnavailable):
        await prepare()
    deps.db.create_workflow_run.assert_not_awaited()


async def test_quota_and_concurrency_are_enforced(deps):
    deps.acquire.return_value = (False,None)
    with pytest.raises(handoff.AgentHandoffUnavailable):
        await prepare()
    deps.db.create_workflow_run.assert_not_awaited()


async def test_duplicate_worker_returns_redundant_slot(deps):
    deps.db.create_workflow_run.side_effect = None
    deps.db.create_workflow_run.return_value = SimpleNamespace(id=501,initial_context={"dialer_concurrency_slot":"slot-original"})
    assert (await prepare()).workflow_run_id == 501
    deps.release.assert_awaited_once_with(11,"slot-a")
    deps.bind.assert_not_awaited()


async def test_capability_check_does_not_reserve_calls_or_quota(deps):
    result = await handoff.agent_handoff_capability(rep_user_id="rep-uuid",account_sid=ACCOUNT)
    assert result == {"available":True,"reason":None}
    deps.db.create_workflow_run.assert_not_awaited()
    deps.acquire.assert_not_awaited()
    deps.provider.initiate_call.assert_not_awaited()


async def test_stream_started_waits_for_authenticated_media(deps):
    deps.db.get_workflow_run = AsyncMock(return_value=SimpleNamespace(
        initial_context={"dialer_original_call_id":CALL},gathered_context={},
    ))
    with pytest.raises(handoff.AgentHandoffUnavailable,match="not been authenticated"):
        await handoff.record_handoff_event(call_id=CALL,event="started",event_at="2026-10-18T12:00:00Z",workflow_run_id="501")
    deps.rpc.assert_not_awaited()
    deps.db.get_workflow_run.return_value.gathered_context={"dialer_media_authenticated":True}
    assert await handoff.record_handoff_event(call_id=CALL,event="started",event_at="2026-10-18T12:00:00Z",workflow_run_id="501")


@pytest.fixture
def media_deps():
    context = {"dialer_agent_request_id":REQUEST,"dialer_original_call_id":CALL,"telephony_configuration_id":77}
    run = SimpleNamespace(id=501,workflow_id=33,state="initialized",initial_context=context)
    workflow = SimpleNamespace(id=33,organization_id=11,user_id=99)
    provider = SimpleNamespace(PROVIDER_NAME="twilio",account_sid=ACCOUNT,auth_token="private-token")
    token = handoff.media_token(auth_token="private-token",run_id=501,call_id=CALL)
    start = {"event":"start","start":{"streamSid":"MZ-test","callSid":CALL,"accountSid":ACCOUNT,"customParameters":{"handoffToken":token},"mediaFormat":{"encoding":"audio/x-mulaw","sampleRate":8000,"channels":1}}}
    socket = SimpleNamespace(receive_json=AsyncMock(side_effect=[{"event":"connected"},start]),close=AsyncMock())
    db = MagicMock()
    db.get_workflow_run = AsyncMock(return_value=run)
    db.get_workflow_by_id = AsyncMock(return_value=workflow)
    db.get_telephony_configuration_for_org = AsyncMock(return_value=SimpleNamespace(provider="twilio"))
    db.claim_dialer_agent_media = AsyncMock(return_value=True)
    db.update_workflow_run = AsyncMock()
    with (
        patch(f"{MOD}.db_client",db),
        patch(f"{MOD}.get_telephony_provider_for_run",new=AsyncMock(return_value=provider)),
        patch(f"{MOD}.release_call_slot",new=AsyncMock()) as release,
        patch(f"{MOD}._run_agent_pipeline",new=AsyncMock()) as pipeline,
        patch(f"{MOD}.record_handoff_event",new=AsyncMock(return_value=True)) as record,
    ):
        yield SimpleNamespace(socket=socket,start=start,db=db,pipeline=pipeline,release=release,record=record)


async def test_valid_media_claims_one_run_and_passes_existing_call_to_pipeline(media_deps):
    d = media_deps
    await handoff.serve_agent_media(d.socket,501)
    d.db.claim_dialer_agent_media.assert_awaited_once_with(501,11,CALL)
    d.pipeline.assert_awaited_once_with(d.socket,provider_name="twilio",workflow_id=33,workflow_run_id=501,user_id=99,call_id=CALL,transport_kwargs={"stream_sid":"MZ-test","call_sid":CALL})
    d.release.assert_awaited_once_with(501)
    assert d.record.await_args.kwargs["event"] == "started"
    assert d.record.await_args.kwargs["call_id"] == CALL
    assert d.record.await_args.kwargs["workflow_run_id"] == "501"


async def test_media_does_not_start_pipeline_when_connection_cannot_be_persisted(media_deps):
    d = media_deps
    d.record.return_value = False
    await handoff.serve_agent_media(d.socket,501)
    d.pipeline.assert_not_awaited()
    d.db.update_workflow_run.assert_awaited_once_with(501,is_completed=True,state="completed")
    d.release.assert_awaited_once_with(501)
    assert d.socket.close.await_args.kwargs["code"] == 1011


@pytest.mark.parametrize("field,value", [("callSid","other-call"),("accountSid","another-account"),("customParameters",{"handoffToken":"forged"})])
async def test_wrong_call_account_or_token_never_claims_media(media_deps,field,value):
    d = media_deps
    d.start["start"][field] = value
    await handoff.serve_agent_media(d.socket,501)
    d.db.claim_dialer_agent_media.assert_not_awaited()
    d.pipeline.assert_not_awaited()
    d.release.assert_not_awaited()
    assert d.socket.close.await_args.kwargs["code"] == 4403


async def test_duplicate_media_socket_never_starts_second_pipeline(media_deps):
    d = media_deps
    d.db.claim_dialer_agent_media.return_value = False
    await handoff.serve_agent_media(d.socket,501)
    d.pipeline.assert_not_awaited()
    d.release.assert_not_awaited()
    assert d.socket.close.await_args.kwargs["code"] == 4409
