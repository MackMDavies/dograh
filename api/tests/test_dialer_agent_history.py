"""Agent history exposes a curated execution only after account authorization."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.services.telephony.dialer import agent_history as history


@pytest.fixture
def deps():
    binding = dict(id="request", client_account_id="account", workflow_run_id="101",
        workflow_uuid="workflow", to_number="+14155550123", role="outbound",
        status="initiated", created_at="2026-10-18T10:00:00Z")
    run = SimpleNamespace(id=101, workflow_id=7, initial_context={
        "dialer_account_id":"account", "called_number":"+14155550123",
        "dialer_agent_request_id":"request"}, state="running", is_completed=False,
        logs={}, gathered_context={}, recording_url=None, storage_backend="s3")
    rpc = AsyncMock(side_effect=lambda name, *_a, **_k:
        binding if name == "dialer_agent_history_binding" else True)
    with (
        patch.object(history.history_db,"rpc",rpc),
        patch.object(history.db_client,"get_workflow_run",AsyncMock(return_value=run)) as get_run,
        patch.object(history.db_client,"get_workflow_by_id",AsyncMock(return_value=SimpleNamespace(workflow_uuid="workflow",organization_id=19))) as get_workflow,
    ):
        yield SimpleNamespace(binding=binding,run=run,rpc=rpc,get_run=get_run,get_workflow=get_workflow)


async def test_authorizes_user_before_loading_private_binding(deps):
    deps.rpc.side_effect = None
    deps.rpc.return_value = False
    with pytest.raises(history.AgentHistoryNotFound):
        await history.details("request","Bearer rep")
    deps.rpc.assert_awaited_once_with("dialer_can_read_agent_call",{"p_call_id":"request"},authorization="Bearer rep")
    deps.get_run.assert_not_awaited()


async def test_rereads_run_with_verified_workflow_organization(deps):
    await history.details("request","Bearer rep")
    assert deps.get_run.await_args_list[0].args == (101,)
    assert deps.get_run.await_args_list[1].kwargs == {"organization_id":19}


@pytest.mark.parametrize("key,value",[
    ("dialer_account_id","other-account"),("called_number","+14155550999"),
    ("dialer_agent_request_id","other-request"),
])
async def test_rejects_mismatched_execution_identity(deps,key,value):
    deps.run.initial_context[key] = value
    with pytest.raises(history.AgentHistoryNotFound):
        await history.details("request","Bearer rep")
    assert all(call.args[0] != "dialer_apply_agent_history_state" for call in deps.rpc.await_args_list)


async def test_rejects_changed_workflow_catalogue_binding(deps):
    deps.get_workflow.return_value.workflow_uuid = "unrelated-workflow"
    with pytest.raises(history.AgentHistoryNotFound):
        await history.details("request","Bearer rep")


async def test_parallel_call_requires_exact_existing_provider_leg(deps):
    deps.binding["provider_call_id"] = "CA-existing"
    deps.run.initial_context["dialer_original_call_id"] = "CA-other"
    with pytest.raises(history.AgentHistoryNotFound):
        await history.details("request","Bearer rep")


async def test_details_only_expose_curated_context_and_final_transcript(deps):
    deps.binding["analysis"] = {"summary":"Qualified prospect", "outcome":"callback", "sentiment":"positive", "status":"completed", "private":"secret"}
    deps.run.gathered_context = {"role":"Director", "pain":"missed calls", "credentials":"secret", "decision_makers":["private-object"]}
    deps.run.logs = {"credentials":"secret", "realtime_feedback_events":[
        None, {"payload":None},
        {"type":"rtf-user-transcription","payload":{"text":"partial", "final":False}},
        {"type":"rtf-user-transcription","payload":{"text":"Please call tomorrow", "final":True}},
        {"type":"rtf-bot-text","payload":{"text":"Of course"}},
    ]}
    result = await history.details("request","Bearer rep")
    assert result["summary"] == "Qualified prospect"
    assert result["captured"] == {"role":"Director", "pain":"missed calls"}
    assert "Please call tomorrow" in result["transcript"] and "Of course" in result["transcript"]
    assert "partial" not in result["transcript"] and "secret" not in str(result)
    assert not {"initial_context","logs","workflow_id","workflow_run_id"} & result.keys()


@pytest.mark.parametrize("path",[None,"recordings/102.wav","https://other.example/audio.wav","../secret"])
async def test_recording_rejects_arbitrary_or_other_run_paths(deps,path):
    deps.run.recording_url = path
    with pytest.raises(history.AgentHistoryNotFound):
        await history.recording("request","Bearer rep")


async def test_recording_reads_only_exact_run_owned_object(deps):
    deps.run.recording_url = "recordings/101.wav"
    storage = SimpleNamespace(aread_bytes=AsyncMock(return_value=b"RIFF-audio"))
    with patch("api.services.storage.get_storage_for_backend",return_value=storage):
        assert await history.recording("request","Bearer rep") == b"RIFF-audio"
    storage.aread_bytes.assert_awaited_once_with("recordings/101.wav")


def test_actual_carrier_terminal_event_finishes_even_before_run_cleanup(deps):
    deps.run.logs = {"telephony_status_callbacks":[
        {"status":"answered","timestamp":"2026-10-18T11:00:00+01:00"},
        {"status":"completed","timestamp":"2026-10-18T10:00:30Z","duration":"30"},
    ]}
    state = history.execution_state(deps.run,deps.binding)
    assert state == dict(status="completed",connected_at="2026-10-18T10:00:00+00:00",
        ended_at="2026-10-18T10:00:30+00:00",provider_outcome="completed",duration_seconds=30)


@pytest.mark.parametrize("value",["NaN", "Infinity", -1, True, {}, None])
def test_never_invents_invalid_provider_duration(deps,value):
    deps.run.logs = {"telephony_status_callbacks":[{"status":"failed","duration":value}]}
    state = history.execution_state(deps.run,deps.binding)
    assert state["duration_seconds"] is None and state["status"] == "failed"


def test_preserves_terminal_binding_and_original_end_time(deps):
    deps.binding.update(status="completed",ended_at="2026-10-18T10:00:30Z",provider_outcome="completed")
    deps.run.logs = {"telephony_status_callbacks":[]}
    state = history.execution_state(deps.run,deps.binding)
    assert state["status"] == "completed" and state["provider_outcome"] == "completed"
    assert state["ended_at"] == "2026-10-18T10:00:30+00:00"


async def test_changed_binding_is_retryable_storage_failure(deps):
    deps.rpc.side_effect = None
    deps.rpc.return_value = False
    with pytest.raises(history.history_db.HistoryStorageUnavailable):
        await history.sync_binding(deps.binding,deps.run)
