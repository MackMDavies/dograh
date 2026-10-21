import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    OutputAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from api.services.workflow.sandbox_repair import workflow_snapshot_hash
from api.services.workflow.test_sandbox_mode import SANDBOX_ANNOTATION
from api.services.workflow.voice_test_capacity import (
    WORKER_PROTOCOL_KEY,
    WORKER_PROTOCOL_VERSION,
)
from api.services.workflow.voice_test_control import VoiceControlError, VoiceSession
from api.services.workflow.voice_test_job import (
    JOB_NAME,
    VoiceJobContext,
    enqueue_voice_job,
    execute_voice_job,
    prepare_voice_job,
    voice_job_key,
)
from api.tests.test_voice_test_control import (
    LEASE,
    SECRET,
    SESSION,
    bound_metadata,
    wav_bytes,
)


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.calls = []
        self.enqueue_job = AsyncMock(return_value=SimpleNamespace(job_id="queued"))

    async def set(self, key, value, nx=False, ex=None):
        self.calls.append((key, nx, ex))
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def eval(self, script, count, key, token):
        if self.values.get(key) == token:
            del self.values[key]
            return 1
        return 0


def setup():
    context = VoiceJobContext(SESSION, LEASE, 185, 42, 9)
    workflow = SimpleNamespace(
        id=185, organization_id=42, user_id=7, template_context_variables={}
    )
    definition = SimpleNamespace(
        workflow_id=185,
        version_number=3,
        status="published",
        workflow_json={"nodes": []},
        workflow_configurations={},
        template_context_variables={},
    )
    data, source, provider_packets = bound_metadata(
        workflow_snapshot_hash(definition.workflow_json, {}, {})
    )
    data["script"].update(start_mode="caller_first", opening_timeout_ms=500)
    data["script"]["steps"][0]["response_timeout_ms"] = 500
    session = VoiceSession.parse(data, SESSION, 185)
    run = SimpleNamespace(
        id=123,
        workflow_id=185,
        mode="smallwebrtc",
        is_completed=False,
        definition=definition,
        annotations={},
    )

    async def create(**kwargs):
        run.annotations = copy.deepcopy(kwargs["annotations"])
        return run

    async def update(run_id, **kwargs):
        assert run_id == run.id
        run.annotations = copy.deepcopy(kwargs.get("annotations", run.annotations))
        return run

    async def save_result(run_id, workflow_id, organization_id, session_id, report):
        assert (run_id, workflow_id, organization_id, session_id) == (
            123,
            185,
            42,
            SESSION,
        )
        run.annotations["agent_test_voice_result"] = copy.deepcopy(report)
        return True

    db = SimpleNamespace(
        get_workflow=AsyncMock(return_value=workflow),
        get_workflow_versions=AsyncMock(return_value=[definition]),
        create_workflow_run=AsyncMock(side_effect=create),
        get_workflow_run_by_id=AsyncMock(return_value=run),
        update_workflow_run=AsyncMock(side_effect=update),
        save_agent_test_voice_result=AsyncMock(side_effect=save_result),
        get_user_configurations=AsyncMock(return_value=source),
        get_default_embedding_model_connection=AsyncMock(return_value=None),
        list_connections=AsyncMock(return_value=[]),
        list_all_connections_superuser=AsyncMock(return_value=[]),
    )
    control = SimpleNamespace(
        get=AsyncMock(return_value=session),
        fixture=AsyncMock(return_value=wav_bytes()),
        claim=AsyncMock(),
        attach=AsyncMock(),
        upload=AsyncMock(),
        finish=AsyncMock(),
        providers=AsyncMock(return_value=provider_packets),
    )
    started = []
    closed = asyncio.Event()

    async def pipeline(transport, actual_workflow, actual_run, provider_configuration):
        assert actual_workflow.id == 185 and actual_run.id == 123
        from api.services.workflow.voice_test_providers import (
            validate_private_voice_provider_configuration,
        )

        assert (
            validate_private_voice_provider_configuration(
                provider_configuration, actual_run, SECRET
            ).llm
            is not None
        )
        started.append(True)
        observer = transport.voice_observer
        transport.pipeline_task = SimpleNamespace(
            stop_when_done=AsyncMock(side_effect=closed.set),
            cancel=AsyncMock(side_effect=lambda **_: closed.set()),
        )

        async def play(encoded):
            assert encoded == wav_bytes()
            observer.observe_output(
                BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM
            )
            at = asyncio.get_running_loop().time()
            frame = OutputAudioRawFrame(b"\x01\x00" * 320, 16000, 1)
            transport.capture.append(frame, at)
            observer.record_audio(at, len(frame.audio))
            await asyncio.sleep(0.025)
            observer.observe_output(
                BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM
            )

        transport.play_wav = play
        await observer.on_pipeline_started()
        await closed.wait()

    return context, db, control, run, pipeline, started, closed


@pytest.mark.asyncio
async def test_queue_is_idempotent_and_never_puts_the_lease_in_logged_job_arguments():
    context, db, control, _, _, _, _ = setup()
    redis = MemoryRedis()
    redis.values[WORKER_PROTOCOL_KEY] = WORKER_PROTOCOL_VERSION
    for _ in range(2):
        assert (await enqueue_voice_job(db, control, redis, context))[
            "status"
        ] == "queued"
    assert len(redis.values) == 2
    assert (
        VoiceJobContext.parse(await redis.get(voice_job_key(SESSION)), SESSION)
        == context
    )
    assert LEASE not in repr(context)
    for call in redis.enqueue_job.await_args_list:
        assert call.args == (JOB_NAME, SESSION)
        assert call.kwargs == {
            "_job_id": f"agent-test-voice:{SESSION}",
            "_expires": 180,
        }
        assert LEASE not in repr(call)
    control.claim.assert_not_awaited()
    db.create_workflow_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_scope_is_checked_before_reading_control_metadata_or_versions():
    context, db, control, _, _, _, _ = setup()
    db.get_workflow.return_value = None
    with pytest.raises(VoiceControlError, match="voice_workflow_not_found"):
        await prepare_voice_job(db, control, context)
    db.get_workflow.assert_awaited_once_with(185, organization_id=42)
    control.get.assert_not_awaited()
    db.get_workflow_versions.assert_not_awaited()


@pytest.mark.asyncio
async def test_conflicting_private_queue_context_is_not_replaced_or_enqueued():
    context, db, control, _, _, _, _ = setup()
    redis = MemoryRedis()
    redis.values[WORKER_PROTOCOL_KEY] = WORKER_PROTOCOL_VERSION
    original = replace(context, organization_id=43).encode()
    redis.values[voice_job_key(SESSION)] = original
    with pytest.raises(VoiceControlError, match="voice_job_context_conflict"):
        await enqueue_voice_job(db, control, redis, context)
    assert redis.values[voice_job_key(SESSION)] == original
    redis.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_new_worker_protocol_blocks_dispatch_before_context_or_job_write():
    context, db, control, _, _, _, _ = setup()
    redis = MemoryRedis()
    with pytest.raises(VoiceControlError, match="voice_worker_unavailable"):
        await enqueue_voice_job(db, control, redis, context)
    assert redis.values == {} and redis.calls == []
    redis.enqueue_job.assert_not_awaited()
    control.claim.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("heartbeat", ["1", b"1", "2", b"2", "4", b"4"])
async def test_an_older_or_unknown_worker_cannot_receive_a_credential_isolated_job(
    heartbeat,
):
    context, db, control, _, _, _, _ = setup()
    redis = MemoryRedis()
    redis.values[WORKER_PROTOCOL_KEY] = heartbeat
    with pytest.raises(VoiceControlError, match="voice_worker_unavailable"):
        await enqueue_voice_job(db, control, redis, context)
    assert redis.calls == []
    redis.enqueue_job.assert_not_awaited()
    control.claim.assert_not_awaited()


@pytest.mark.parametrize(
    "changes",
    [
        {"lease_token": "../bad"},
        {"workflow_id": True},
        {"organization_id": 0},
        {"requested_by_user_id": "9"},
        {"session_id": LEASE},
    ],
)
def test_private_context_is_typed_bounded_and_bound_to_its_session(changes):
    value = {
        **json.loads(VoiceJobContext(SESSION, LEASE, 185, 42, 9).encode()),
        **changes,
    }
    with pytest.raises(VoiceControlError):
        VoiceJobContext.parse(json.dumps(value), SESSION)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["script", "source", "fixture", "claim"])
async def test_preflight_and_uncertain_claim_never_start_providers(bad):
    context, db, control, _, pipeline, started, _ = setup()
    if bad == "script":
        control.get.return_value = replace(control.get.return_value, script=None)
    elif bad == "source":
        db.get_workflow_versions.return_value[0].workflow_json = {
            "nodes": [{"id": "changed"}]
        }
    elif bad == "fixture":
        control.fixture.side_effect = VoiceControlError("voice_fixture_invalid")
    else:
        control.claim.side_effect = VoiceControlError("voice_control_unavailable")
    with pytest.raises((VoiceControlError, ValueError)):
        await execute_voice_job(db, control, context, SECRET, pipeline_runner=pipeline)
    assert started == []
    db.create_workflow_run.assert_not_awaited()
    control.attach.assert_not_awaited()
    control.finish.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_then_private_creation_attestation_attachment_playback_and_sealing():
    context, db, control, run, pipeline, started, closed = setup()
    result = await execute_voice_job(
        db, control, context, SECRET, pipeline_runner=pipeline
    )
    assert result["status"] == "completed" and result["quality_evaluated"] is False
    assert result["cleanup_verified"] is True and started == [True] and closed.is_set()
    marker = db.create_workflow_run.await_args.kwargs["annotations"][SANDBOX_ANNOTATION]
    assert marker["enabled"] is True and marker["voice_session_id"] == SESSION
    assert db.create_workflow_run.await_args.kwargs["user_id"] == 7
    assert (
        run.annotations[SANDBOX_ANNOTATION]["workflow_binding"]["configuration_hash"]
        == control.get.return_value.runtime_workflow_hash
    )
    control.claim.assert_awaited_once()
    nonce = control.claim.await_args.args[2]
    control.attach.assert_awaited_once_with(control.get.return_value, LEASE, nonce, 123)
    control.upload.assert_awaited_once()
    sealed = control.finish.await_args.kwargs
    assert sealed["status"] == "completed" and sealed["duration_ms"] > 0
    assert sealed["duration_ms"] < 30000 and sealed["error_code"] is None
    assert "signature" not in json.dumps(result) and LEASE not in json.dumps(result)
    assert (
        run.annotations["agent_test_voice_result"]["rehearsal"] == result["rehearsal"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["running", "completed", "failed", "expired"])
async def test_redelivered_job_never_restarts_a_claimed_or_terminal_call(state):
    context, db, control, _, pipeline, started, _ = setup()
    control.get.return_value = replace(control.get.return_value, status=state)
    result = await execute_voice_job(
        db, control, context, SECRET, pipeline_runner=pipeline
    )
    assert result["status"] == state and result["cleanup_verified"] is True
    control.claim.assert_not_awaited()
    control.fixture.assert_not_awaited()
    db.create_workflow_run.assert_not_awaited()
    assert started == []


@pytest.mark.asyncio
async def test_attach_failure_is_sealed_failed_without_any_provider_start():
    context, db, control, _, pipeline, started, _ = setup()
    control.attach.side_effect = VoiceControlError(
        "voice_runtime_registration_conflict"
    )
    result = await execute_voice_job(
        db, control, context, SECRET, pipeline_runner=pipeline
    )
    assert result["status"] == "failed" and started == []
    assert result["error_code"] == "voice_runtime_registration_conflict"
    control.upload.assert_not_awaited()
    assert control.finish.await_args.kwargs["status"] == "failed"


@pytest.mark.asyncio
async def test_job_cancellation_stops_the_child_and_settles_before_propagating():
    context, db, control, _, _, started, closed = setup()

    async def waiting_pipeline(transport, *_):
        started.append(True)
        transport.pipeline_task = SimpleNamespace(
            stop_when_done=AsyncMock(side_effect=closed.set),
            cancel=AsyncMock(side_effect=lambda **_: closed.set()),
        )
        await transport.voice_observer.on_pipeline_started()
        await closed.wait()

    running = asyncio.create_task(
        execute_voice_job(
            db, control, context, SECRET, pipeline_runner=waiting_pipeline
        )
    )
    for _ in range(50):
        if started:
            break
        await asyncio.sleep(0.01)
    assert started
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 2)
    assert closed.is_set()
    assert control.finish.await_args.kwargs["status"] == "failed"
    assert control.finish.await_args.kwargs["error_code"] == "voice_job_interrupted"


@pytest.mark.asyncio
async def test_report_persistence_failure_is_settled_failed_and_never_becomes_success():
    context, db, control, _, pipeline, _, _ = setup()
    db.save_agent_test_voice_result.side_effect = None
    db.save_agent_test_voice_result.return_value = False
    result = await execute_voice_job(
        db, control, context, SECRET, pipeline_runner=pipeline
    )
    assert (
        result["status"] == "failed"
        and result["error_code"] == "voice_report_persistence_failed"
    )
    assert control.finish.await_args.kwargs["status"] == "failed"
    assert control.finish.await_args.kwargs["retain_audio"] is True


@pytest.mark.asyncio
async def test_output_readback_failure_is_settled_failed_without_claiming_an_audio_recording():
    context, db, control, _, pipeline, _, _ = setup()
    control.upload.side_effect = VoiceControlError("voice_output_readback_invalid")
    result = await execute_voice_job(
        db, control, context, SECRET, pipeline_runner=pipeline
    )
    assert (
        result["status"] == "failed"
        and result["error_code"] == "voice_output_readback_invalid"
    )
    db.save_agent_test_voice_result.assert_not_awaited()
    assert control.finish.await_args.kwargs["status"] == "failed"
    assert control.finish.await_args.kwargs["retain_audio"] is False


@pytest.mark.asyncio
async def test_an_idempotency_collision_cannot_relabel_a_live_customer_call_as_a_test():
    context, db, control, run, pipeline, started, _ = setup()
    db.create_workflow_run.side_effect = None
    db.create_workflow_run.return_value = run
    result = await execute_voice_job(
        db, control, context, SECRET, pipeline_runner=pipeline
    )
    assert result["error_code"] == "voice_runtime_binding_invalid" and started == []
    assert run.annotations == {}
    db.update_workflow_run.assert_not_awaited()
    control.attach.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_owner", [False, True])
async def test_native_pipeline_rechecks_scope_before_provider_reads(
    monkeypatch, changed_owner
):
    from fastapi import HTTPException

    from api.services.pipecat import run_pipeline as pipeline_module

    workflow = SimpleNamespace(id=185, organization_id=42, user_id=7)
    run = SimpleNamespace(id=123, is_completed=False)
    scoped = AsyncMock(
        return_value=SimpleNamespace(user_id=8) if changed_owner else None
    )
    unscoped = AsyncMock()
    provider_read = AsyncMock()
    provider_start = Mock()
    monkeypatch.setattr(pipeline_module.db_client, "get_workflow", scoped)
    monkeypatch.setattr(pipeline_module.db_client, "get_workflow_by_id", unscoped)
    monkeypatch.setattr(
        pipeline_module.db_client, "get_user_configurations", provider_read
    )
    monkeypatch.setattr(pipeline_module, "create_llm_service", provider_start)
    monkeypatch.setattr(
        pipeline_module, "validate_sandbox_pipeline_binding", lambda *args: True
    )
    from api.services.workflow.voice_test_job import _native_pipeline

    with pytest.raises(HTTPException, match="Private voice workflow scope changed"):
        await _native_pipeline(object(), workflow, run)
    scoped.assert_awaited_once_with(185, organization_id=42)
    unscoped.assert_not_awaited()
    provider_read.assert_not_awaited()
    provider_start.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "no_plan",
        "no_profiles",
        "bad_signature",
        "wrong_revision",
        "client_endpoint",
        "changed_behavior",
    ],
)
async def test_invalid_test_credentials_block_before_claim_runtime_write_or_provider_start(
    bad,
):
    from api.tests.test_voice_test_providers import sign

    context, db, control, _, pipeline, started, _ = setup()
    session = control.get.return_value
    if bad == "no_plan":
        control.get.return_value = replace(session, provider_plan=None)
    elif bad == "no_profiles":
        control.providers.return_value = []
    elif bad == "bad_signature":
        control.providers.return_value[0]["fingerprint"] = "f" * 64
    elif bad == "wrong_revision":
        control.providers.return_value[0]["revision"] += 1
        control.providers.return_value[0] = sign(
            control.providers.return_value[0], SECRET
        )
    elif bad == "client_endpoint":
        packet = control.providers.return_value[0]
        packet["settings"]["base_url"] = "https://client.example.invalid/notify"
        packet = sign(packet, SECRET)
        control.providers.return_value[0] = packet
        session.provider_plan["bindings"][packet["service_type"]]["fingerprint"] = (
            packet["fingerprint"]
        )
    else:
        session.provider_plan["services"]["llm"]["temperature"] = {
            "invalid": "not a temperature"
        }
    with pytest.raises(VoiceControlError):
        await execute_voice_job(db, control, context, SECRET, pipeline_runner=pipeline)
    control.claim.assert_not_awaited()
    control.attach.assert_not_awaited()
    control.fixture.assert_not_awaited()
    db.create_workflow_run.assert_not_awaited()
    assert not started


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "model",
        "route",
        "owner",
        "missing_intent",
        "intent_hash",
        "binding_hash",
        "plan_behavior",
    ],
)
async def test_current_source_and_frozen_plan_must_match_before_credentials_or_paid_claim(
    change,
):
    context, db, control, _, pipeline, started, _ = setup()
    session = control.get.return_value
    if change in {"model", "route"}:
        source = db.get_user_configurations.return_value
        source.llm = source.llm.model_copy(
            update={
                "model" if change == "model" else "base_url": "changed-model"
                if change == "model"
                else "https://private-customer.invalid/model"
            }
        )
    elif change == "owner":
        db.get_workflow.return_value.user_id = 8
    elif change == "missing_intent":
        control.get.return_value = replace(session, provider_intent=None)
    elif change == "intent_hash":
        control.get.return_value = replace(session, provider_intent_hash="0" * 64)
    elif change == "binding_hash":
        control.get.return_value = replace(session, provider_binding_hash="0" * 64)
    else:
        session.provider_plan["services"]["llm"]["model"] = "substituted-model"
    with pytest.raises(VoiceControlError) as error:
        await execute_voice_job(db, control, context, SECRET, pipeline_runner=pipeline)
    assert "private-customer" not in str(error.value)
    control.providers.assert_not_awaited()
    control.fixture.assert_not_awaited()
    control.claim.assert_not_awaited()
    db.create_workflow_run.assert_not_awaited()
    assert not started


@pytest.mark.asyncio
async def test_frozen_qa_execution_does_not_depend_on_the_owners_production_key():
    context, db, control, _, pipeline, _, _ = setup()
    source = db.get_user_configurations.return_value
    source.llm = source.llm.model_copy(
        update={"api_key": "SYNTHETIC_PRODUCTION_VALUE_NEVER_FOR_SDK"}
    )
    result = await execute_voice_job(
        db, control, context, SECRET, pipeline_runner=pipeline
    )
    assert result["status"] == "completed"
    control.claim.assert_awaited_once()
