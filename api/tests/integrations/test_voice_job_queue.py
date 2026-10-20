"""Real private Redis/ARQ durability; speech and control services are test fixtures."""

import asyncio
import hashlib
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from arq.connections import RedisSettings, create_pool
from arq.jobs import Job
from arq.worker import Worker
from pipecat.frames.frames import InputAudioRawFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.tests import MockLLMService, MockTTSService
from pipecat.utils.time import time_now_iso8601

from api.services.workflow.sandbox_repair import workflow_snapshot_hash
from api.services.workflow.voice_test_capacity import (
    WORKER_PROTOCOL_KEY,
    claim_voice_capacity,
    private_voice_capabilities,
    publish_voice_worker_protocol,
    release_voice_capacity,
)
from api.services.workflow.voice_test_control import (
    VoiceControlError,
    VoiceSession,
    output_measurements,
)
from api.services.workflow.voice_test_job import (
    JOB_NAME,
    VoiceJobContext,
    enqueue_voice_job,
    execute_voice_job,
    read_voice_job_status,
    voice_job_key,
)
from api.tasks import agent_test_voice
from api.tests.integrations._run_pipeline_helpers import (
    create_workflow_run_rows,
    patch_run_pipeline_externals,
)
from api.tests.integrations.test_run_pipeline_text_greeting import WORKFLOW_DEFINITION
from api.tests.test_sandbox_audio_transport import wav_fixture
from api.tests.test_voice_test_control import LEASE, SECRET, SESSION, metadata
from api.tests.test_voice_test_job import setup


@pytest_asyncio.fixture
async def private_queue():
    executable = shutil.which("redis-server")
    if executable is None:
        pytest.skip(
            "An isolated local Redis binary is required for the durable queue proof"
        )
    with tempfile.TemporaryDirectory(prefix="svvq-", dir="/private/tmp") as directory:
        socket = Path(directory) / "queue.sock"
        process = await asyncio.create_subprocess_exec(
            executable,
            "--port",
            "0",
            "--unixsocket",
            str(socket),
            "--unixsocketperm",
            "700",
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            directory,
            "--loglevel",
            "warning",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        pool = None
        try:
            for _ in range(100):
                if socket.exists() or process.returncode is not None:
                    break
                await asyncio.sleep(0.02)
            assert process.returncode is None and socket.exists(), (
                "The private Redis test process did not start"
            )
            settings = RedisSettings(unix_socket_path=str(socket), conn_retries=0)
            pool = await create_pool(settings)
            assert await pool.ping()
            yield settings, pool
        finally:
            if pool is not None:
                await pool.aclose()
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.wait(), 5)


@pytest.mark.asyncio
async def test_separate_api_and_worker_connections_execute_one_durable_private_job(
    private_queue, monkeypatch
):
    settings, api_pool = private_queue
    context, db, control, run, pipeline, started, _ = setup()
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", SECRET)
    assert await publish_voice_worker_protocol({"redis": api_pool})
    original = control.get.return_value

    async def finish(*_, **kwargs):
        assert kwargs["status"] == "completed"
        report = run.annotations["agent_test_voice_result"]
        control.get.return_value = replace(
            original,
            status="completed",
            runtime_workflow_run_id=123,
            actual_duration_ms=report["actual_duration_ms"],
            audio_sha256=report["audio_sha256"],
            audio_bytes=report["audio_bytes"],
        )

    control.finish.side_effect = finish

    async def execute(_db, _control, job_context, secret):
        return await execute_voice_job(
            db, control, job_context, secret, pipeline_runner=pipeline
        )

    monkeypatch.setattr(agent_test_voice, "execute_voice_job", execute)
    await enqueue_voice_job(db, control, api_pool, context)
    await enqueue_voice_job(db, control, api_pool, context)
    job_id = f"agent-test-voice:{SESSION}"
    info = await Job(job_id, api_pool).info()
    assert info.args == (SESSION,) and info.kwargs == {}
    raw_job = await api_pool.get(f"arq:job:{job_id}")
    assert LEASE.encode() not in raw_job and SECRET.encode() not in raw_job
    assert await api_pool.ttl(voice_job_key(SESSION)) > 0
    # Close the producer connection before starting a separate worker. The
    # queued work and private capability must survive this process boundary.
    await api_pool.aclose()
    worker_pool = await create_pool(settings)
    worker = Worker(
        [agent_test_voice.run_agent_test_voice],
        redis_pool=worker_pool,
        burst=True,
        handle_signals=False,
        poll_delay=0.01,
        log_results=False,
    )
    try:
        await worker.main()
        assert worker.jobs_complete == 1 and worker.jobs_failed == 0
        assert started == [True]
        control.claim.assert_awaited_once()
        saved = await Job(job_id, worker_pool).result_info()
        assert saved.success and saved.result["status"] == "completed"
        assert LEASE not in repr(saved.result) and SECRET not in repr(saved.result)
        report = await read_voice_job_status(db, control, worker_pool, context, SECRET)
        assert (
            report["report_status"] == "verified"
            and report["quality_evaluated"] is False
        )
        assert await worker_pool.exists("agent-test-voice-slot:0") == 0
        # Simulate loss of the queue acknowledgement after native settlement.
        # Redelivery reads the sealed control state and cannot buy another call.
        await worker_pool.delete(f"arq:result:{job_id}")
        await worker_pool.enqueue_job(JOB_NAME, SESSION, _job_id=job_id)
        await worker.main()
        assert started == [True] and control.claim.await_count == 1
        assert db.create_workflow_run.await_count == 1
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_capacity_is_atomic_across_real_redis_clients_and_checks_owner_on_release(
    private_queue,
):
    settings, first = private_queue
    second = await create_pool(settings)
    try:
        slots = await asyncio.gather(
            claim_voice_capacity(first),
            claim_voice_capacity(second),
            claim_voice_capacity(first),
        )
        assert len({slot[0] for slot in slots}) == 3
        assert 350 <= await second.ttl(slots[0][0]) <= 360
        with pytest.raises(VoiceControlError, match="voice_worker_capacity_full"):
            await claim_voice_capacity(second)
        assert not await release_voice_capacity(second, (slots[0][0], "wrong-owner"))
        assert await release_voice_capacity(second, slots[0])
        new = await claim_voice_capacity(first)
        assert new[0] == slots[0][0] and new[1] != slots[0][1]
        assert not await release_voice_capacity(first, slots[0])
        assert await release_voice_capacity(second, new)
    finally:
        await second.aclose()


class FixtureSpeechRecognition(FrameProcessor):
    """Map a known synthetic PCM input to a fixed transcript; no ASR claim."""

    def __init__(self):
        super().__init__()
        self.receiving = False
        self.recognized = 0

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            if any(frame.audio):
                self.receiving = True
            elif self.receiving:
                self.receiving = False
                self.recognized += 1
                await self.push_frame(
                    TranscriptionFrame(
                        "What are your opening hours?",
                        "synthetic-caller",
                        time_now_iso8601(),
                    )
                )
        await self.push_frame(frame, direction)


@pytest.mark.asyncio
async def test_durable_worker_drives_native_workflow_and_persists_private_report(
    private_queue, db_session, async_session, monkeypatch
):
    """Real queue, SQL rows and native pipeline; external speech/control are fixtures."""
    _, redis = private_queue
    _, user, workflow = await create_workflow_run_rows(
        db_session,
        async_session,
        workflow_definition=WORKFLOW_DEFINITION,
        name_prefix="Durable Native Voice",
        provider_id_suffix="durable-native-voice",
    )
    definitions = await db_session.get_workflow_versions(workflow.id, limit=None)
    definition = next(item for item in definitions if item.status == "published")
    encoded, _ = wav_fixture()
    data = metadata()
    data.update(
        workflow_id=workflow.id,
        workflow_version=definition.version_number,
        runtime_workflow_hash=workflow_snapshot_hash(
            definition.workflow_json,
            definition.workflow_configurations,
            definition.template_context_variables,
        ),
    )
    data["fixture_manifest"][0].update(
        sha256=hashlib.sha256(encoded).hexdigest(), duration_ms=80
    )
    data["script"].update(start_mode="agent_first", opening_timeout_ms=10000)
    data["script"]["steps"][0]["response_timeout_ms"] = 10000
    session = VoiceSession.parse(data, SESSION, workflow.id)
    from types import SimpleNamespace

    control = SimpleNamespace(
        get=AsyncMock(return_value=session),
        fixture=AsyncMock(return_value=encoded),
        claim=AsyncMock(),
        attach=AsyncMock(),
        upload=AsyncMock(),
        finish=AsyncMock(),
    )
    audio = []

    async def upload(_session, _lease, _nonce, recording):
        measured = output_measurements(recording, session.duration_seconds)
        assert measured["pcm_bytes"] > 0
        audio.append(recording)
        return measured

    async def finish(*_, **kwargs):
        assert kwargs["status"] == "completed", kwargs["error_code"]
        runtime_id = control.attach.await_args.args[3]
        saved = await db_session.get_workflow_run_by_id(runtime_id)
        report = saved.annotations["agent_test_voice_result"]
        control.get.return_value = replace(
            session,
            status="completed",
            runtime_workflow_run_id=runtime_id,
            actual_duration_ms=report["actual_duration_ms"],
            audio_sha256=report["audio_sha256"],
            audio_bytes=report["audio_bytes"],
        )

    control.upload.side_effect = upload
    control.finish.side_effect = finish
    monkeypatch.setenv("SYSEVO_MEMORY_SECRET", SECRET)
    monkeypatch.setattr(agent_test_voice, "db_client", db_session)
    monkeypatch.setattr(agent_test_voice, "VoiceControlClient", lambda *_: control)
    context = VoiceJobContext(
        SESSION, LEASE, workflow.id, workflow.organization_id, user.id
    )
    assert await publish_voice_worker_protocol({"redis": redis})
    await enqueue_voice_job(db_session, control, redis, context)
    llm = MockLLMService(
        mock_steps=[
            MockLLMService.create_text_chunks(
                "We are open Monday to Friday, nine to five."
            )
        ],
        chunk_delay=0.001,
    )
    tts = MockTTSService(mock_audio_duration_ms=160, frame_delay=0)
    recognition = FixtureSpeechRecognition()
    captured = []
    worker = Worker(
        [agent_test_voice.run_agent_test_voice],
        redis_pool=redis,
        burst=True,
        handle_signals=False,
        poll_delay=0.01,
        log_results=False,
    )
    try:
        with patch_run_pipeline_externals(captured, llm=llm, tts=tts):
            monkeypatch.setattr(
                "api.services.pipecat.run_pipeline.create_stt_service",
                lambda *_args, **_kwargs: recognition,
            )
            async with asyncio.timeout(50):
                await worker.main()
        assert worker.jobs_complete == 1 and worker.jobs_failed == 0
        info = await Job(f"agent-test-voice:{SESSION}", redis).result_info()
        assert info.result["status"] == "completed", info.result["error_code"]
        assert recognition.recognized == 1 and llm.get_current_step() == 1
        assert len(tts.received_texts) >= 2 and len(audio) == 1
        runtime_id = info.result["runtime_workflow_run_id"]
        native = await db_session.get_workflow_run_by_id(runtime_id)
        assert (
            native.is_completed
            and native.annotations["sysevo_test_sandbox"]["voice_session_id"] == SESSION
        )
        assert (
            await db_session.get_workflow_run(
                runtime_id, organization_id=workflow.organization_id
            )
            is None
        )
        assert await db_session.ensure_public_access_token(runtime_id) is None
        status = await read_voice_job_status(
            db_session, control, redis, context, SECRET
        )
        assert status["report_status"] == "verified" and not status["quality_evaluated"]
        assert (
            native.annotations["agent_test_voice_result"]["audio_sha256"]
            == hashlib.sha256(audio[0]).hexdigest()
        )
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_private_capabilities_follow_real_worker_heartbeat_expiry(private_queue):
    _, redis = private_queue
    assert (await private_voice_capabilities(redis))["worker_ready"] is False
    assert await publish_voice_worker_protocol({"redis": redis})
    assert 0 < await redis.ttl(WORKER_PROTOCOL_KEY) <= 90
    assert (await private_voice_capabilities(redis))["worker_ready"] is True
    await redis.pexpire(WORKER_PROTOCOL_KEY, 20)
    for _ in range(50):
        if await redis.get(WORKER_PROTOCOL_KEY) is None:
            break
        await asyncio.sleep(0.02)
    assert (await private_voice_capabilities(redis))["worker_ready"] is False
