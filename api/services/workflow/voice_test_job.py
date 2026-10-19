"""Durable admission and one-time execution of private native voice rehearsals."""

import asyncio
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from uuid import uuid4

from api.services.workflow.test_sandbox_binding import (
    make_sandbox_workflow_binding,
    resolve_requested_sandbox_definition,
    validate_sandbox_pipeline_binding,
)
from api.services.workflow.test_sandbox_mode import SANDBOX_ANNOTATION
from api.services.workflow.voice_test_capacity import (
    WORKER_PROTOCOL_KEY,
    WORKER_PROTOCOL_VERSION,
)
from api.services.workflow.voice_test_control import (
    VoiceControlError,
    canonical_uuid,
    positive_int,
)
from api.services.workflow.voice_test_providers import (
    build_private_user_configuration,
    private_voice_source_from_plan,
    seal_private_voice_provider_configuration,
    validate_private_voice_provider_plan,
)
from api.services.workflow.voice_test_result import (
    validate_rehearsal,
    validate_voice_report,
)

JOB_NAME = "run_agent_test_voice_v2"
CONTEXT_TTL_SECONDS = 600
JOB_EXPIRY_SECONDS = 180


class VoiceProviderPreflightError(VoiceControlError):
    """Credential preparation failed before a paid claim or provider start."""


def voice_job_key(session_id):
    return f"agent-test-voice-context:{canonical_uuid(session_id)}"


@dataclass(frozen=True, repr=False)
class VoiceJobContext:
    session_id: str
    lease_token: str
    workflow_id: int
    organization_id: int
    requested_by_user_id: int

    def __repr__(self):
        return "VoiceJobContext()"

    def encode(self):
        canonical_uuid(self.session_id)
        canonical_uuid(self.lease_token)
        if not all(
            positive_int(value)
            for value in (
                self.workflow_id,
                self.organization_id,
                self.requested_by_user_id,
            )
        ):
            raise VoiceControlError("voice_job_context_invalid")
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def parse(cls, encoded, session_id):
        try:
            if not isinstance(encoded, (str, bytes)) or len(encoded) > 2048:
                raise ValueError
            value = json.loads(encoded)
            if not isinstance(value, dict) or set(value) != {
                "session_id",
                "lease_token",
                "workflow_id",
                "organization_id",
                "requested_by_user_id",
            }:
                raise ValueError
            context = cls(**value)
            context.encode()
            if context.session_id != canonical_uuid(session_id):
                raise ValueError
            return context
        except (ValueError, TypeError, UnicodeError):
            raise VoiceControlError("voice_job_context_invalid") from None


async def prepare_voice_job(db, control, context):
    # Repeated by the worker. Never read version/configuration or control
    # metadata for an organization before checking its workflow ownership.
    context.encode()
    workflow = await db.get_workflow(
        context.workflow_id, organization_id=context.organization_id
    )
    if workflow is None:
        raise VoiceControlError("voice_workflow_not_found", 404)
    if not positive_int(workflow.user_id):
        raise VoiceControlError("voice_runtime_owner_missing")
    session = await control.get(
        context.session_id, context.lease_token, context.workflow_id
    )
    if session.script is None:
        raise VoiceControlError("voice_script_missing")
    try:
        definition = await resolve_requested_sandbox_definition(
            db,
            workflow_id=context.workflow_id,
            organization_id=context.organization_id,
            version_number=session.workflow_version,
            version_kind=session.version_kind,
            expected_hash=session.runtime_workflow_hash,
        )
    except ValueError:
        raise VoiceControlError("voice_source_changed") from None
    return workflow, session, definition


async def enqueue_voice_job(db, control, redis, context):
    _, session, _ = await prepare_voice_job(db, control, context)
    if session.status != "reserved":
        return {"status": session.status, "session_id": session.id}
    encoded = context.encode()
    key = voice_job_key(context.session_id)
    try:
        async with asyncio.timeout(10):
            if await redis.get(WORKER_PROTOCOL_KEY) not in (
                WORKER_PROTOCOL_VERSION,
                WORKER_PROTOCOL_VERSION.encode(),
            ):
                raise VoiceControlError("voice_worker_unavailable")
            saved = await redis.set(key, encoded, nx=True, ex=CONTEXT_TTL_SECONDS)
            if not saved:
                current = await redis.get(key)
                if isinstance(current, bytes):
                    current = current.decode("utf-8")
                if current != encoded:
                    raise VoiceControlError("voice_job_context_conflict")
            # ARQ may log arguments. The lease lives in a separate private,
            # expiring Redis value, never in job arguments or job results.
            await redis.enqueue_job(
                JOB_NAME,
                session.id,
                _job_id=f"agent-test-voice:{session.id}",
                _expires=JOB_EXPIRY_SECONDS,
            )
    except VoiceControlError:
        raise
    except Exception:  # noqa: BLE001 - the queue boundary returns only redacted fixed codes
        raise VoiceControlError("voice_queue_unavailable", 503) from None
    return {"status": "queued", "session_id": session.id}


async def _native_pipeline(transport, workflow, run, private_voice_providers=None):
    from api.services.pipecat.audio_config import create_audio_config
    from api.services.pipecat.run_pipeline import _run_pipeline

    await _run_pipeline(
        transport,
        workflow.id,
        run.id,
        workflow.user_id,
        audio_config=create_audio_config("smallwebrtc"),
        workflow_run=run,
        expected_organization_id=workflow.organization_id,
        private_voice_providers=private_voice_providers,
    )


async def _stop_pipeline(pipeline, transport, success):
    if pipeline is None:
        return
    task = transport.pipeline_task
    try:
        async with asyncio.timeout(10):
            if not pipeline.done():
                if task is not None:
                    if success:
                        await task.stop_when_done()
                    else:
                        await task.cancel(reason="private_voice_rehearsal_finished")
                else:
                    pipeline.cancel()
            await pipeline
    except asyncio.CancelledError:
        # Only the explicitly cancelled child may finish this way. A cancelled
        # parent must still propagate through execute_voice_job's finally.
        if asyncio.current_task().cancelling() or not pipeline.done():
            raise
    except Exception:  # noqa: BLE001 - cleanup must handle arbitrary provider SDK failures
        pipeline.cancel()
        try:
            async with asyncio.timeout(5):
                await pipeline
        except (Exception, asyncio.CancelledError):  # noqa: BLE001 - redact and quarantine below
            raise VoiceControlError("voice_pipeline_cleanup_unverified") from None
        raise VoiceControlError("voice_pipeline_cleanup_unverified") from None


async def execute_voice_job(
    db, control, context, signing_secret, *, pipeline_runner=None
):
    from api.services.pipecat.sandbox_audio_transport import SandboxAudioTransport
    from api.services.pipecat.voice_test_rehearsal import (
        VoiceTurnObserver,
        run_voice_script,
    )

    if not signing_secret:
        raise VoiceControlError("voice_control_not_configured")
    async with asyncio.timeout(60):
        workflow, session, _ = await prepare_voice_job(db, control, context)
        if session.status != "reserved":
            # ARQ retries after a crash cannot claim a second paid execution.
            return {
                "status": session.status,
                "session_id": session.id,
                "cleanup_verified": True,
            }
        try:
            provider_plan = validate_private_voice_provider_plan(session.provider_plan)
            provider_packets = await control.providers(session, context.lease_token)
            provider_source = private_voice_source_from_plan(provider_plan)
            # Validate all QA credentials before the one-time paid claim.
            build_private_user_configuration(
                provider_source,
                provider_packets,
                signing_secret,
                provider_plan["bindings"],
            )
        except VoiceControlError as exc:
            raise VoiceProviderPreflightError(exc.code, exc.status_code) from None
        fixtures = {}
        for fixture in session.fixtures:
            fixtures[fixture.file_name] = await control.fixture(
                session, context.lease_token, fixture
            )
    nonce = str(uuid4())
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    # An uncertain claim response must never start a provider. Its database
    # reservation is reconciled conservatively by the existing reaper.
    await control.claim(session, context.lease_token, nonce)
    deadline = started_at + session.duration_seconds
    transport = SandboxAudioTransport(duration_limit=session.duration_seconds)
    observer = VoiceTurnObserver(transport.output())
    transport.set_voice_observer(observer)
    pipeline = None
    driver = None
    run = None
    result = None
    error = None
    cancelled = False
    stopped = False
    try:
        async with asyncio.timeout_at(deadline):
            marker = {
                "enabled": True,
                "source": "agent_testing_engine",
                "agent_test_run_id": session.run_id,
                "scenario_id": session.scenario_id,
                "voice_session_id": session.id,
                "organization_id": context.organization_id,
                "requested_by_user_id": context.requested_by_user_id,
            }
            run = await db.create_workflow_run(
                name=f"Private voice {session.id}",
                workflow_id=workflow.id,
                mode="smallwebrtc",
                user_id=workflow.user_id,
                organization_id=context.organization_id,
                definition_version_number=session.workflow_version,
                definition_version_status=session.version_kind,
                idempotency_key=f"agent-test-voice:{session.id}",
                initial_context={
                    "agent_test_run_id": session.run_id,
                    "scenario_id": session.scenario_id,
                },
                annotations={SANDBOX_ANNOTATION: marker},
            )
            run = await db.get_workflow_run_by_id(run.id)
            if run is None or run.workflow_id != workflow.id or run.is_completed:
                raise VoiceControlError("voice_runtime_binding_invalid")
            saved_marker = (run.annotations or {}).get(SANDBOX_ANNOTATION)
            if not isinstance(saved_marker, dict) or any(
                saved_marker.get(key) != value for key, value in marker.items()
            ):
                raise VoiceControlError("voice_runtime_binding_invalid")
            marker["workflow_binding"] = make_sandbox_workflow_binding(
                run.definition,
                workflow_id=workflow.id,
                workflow_run_id=run.id,
                run_id=session.run_id,
                scenario_id=session.scenario_id,
                expected_hash=session.runtime_workflow_hash,
                signing_secret=signing_secret,
            )
            await db.update_workflow_run(
                run.id, annotations={SANDBOX_ANNOTATION: marker}
            )
            run = await db.get_workflow_run_by_id(run.id)
            validate_sandbox_pipeline_binding(run, workflow.id, run.id, signing_secret)
            await control.attach(session, context.lease_token, nonce, run.id)
            provider_configuration = seal_private_voice_provider_configuration(
                provider_source,
                provider_packets,
                signing_secret,
                provider_plan["bindings"],
                run,
            )
            pipeline = asyncio.create_task(
                (pipeline_runner or _native_pipeline)(
                    transport, workflow, run, provider_configuration
                )
            )
            driver = asyncio.create_task(
                run_voice_script(session, fixtures, transport, observer)
            )
            done, _ = await asyncio.wait(
                {pipeline, driver}, return_when=asyncio.FIRST_COMPLETED
            )
            if pipeline in done:
                pipeline.result()
                # Give a just-completed final audio segment a chance to wake
                # the driver; an early native completion is not a test pass.
                await asyncio.sleep(0)
                if not driver.done():
                    raise VoiceControlError("voice_pipeline_ended_early")
            result = await driver
    except asyncio.CancelledError:
        cancelled = True
        error = "voice_job_interrupted"
    except TimeoutError:
        error = "voice_duration_limit_reached"
    except VoiceControlError as exc:
        error = exc.code
    except Exception:  # noqa: BLE001 - arbitrary provider exceptions cannot enter job results
        error = "voice_pipeline_failed"
    finally:
        if driver is not None and not driver.done():
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)
        try:
            await _stop_pipeline(
                pipeline, transport, result is not None and error is None
            )
            stopped = True
        except asyncio.CancelledError:
            cancelled = True
            error = "voice_pipeline_cleanup_unverified"
        except VoiceControlError:
            error = "voice_pipeline_cleanup_unverified"
        elapsed_ms = math.ceil((loop.time() - started_at) * 1000) if stopped else None
        if elapsed_ms is not None and elapsed_ms > session.duration_seconds * 1000:
            error = "voice_duration_limit_reached"
        audio_verified = False
        try:
            async with asyncio.timeout(15):
                if error is None:
                    audio = transport.capture.wav_bytes()
                    await control.upload(session, context.lease_token, nonce, audio)
                    audio_verified = True
                    validate_rehearsal(result, session)
                    report = {
                        "version": 1,
                        "session_id": session.id,
                        "run_id": session.run_id,
                        "workflow_id": workflow.id,
                        "organization_id": context.organization_id,
                        "runtime_workflow_run_id": run.id,
                        "workflow_version": session.workflow_version,
                        "version_kind": session.version_kind,
                        "configuration_hash": session.configuration_hash,
                        "runtime_workflow_hash": session.runtime_workflow_hash,
                        "actual_duration_ms": elapsed_ms,
                        "audio_sha256": hashlib.sha256(audio).hexdigest(),
                        "audio_bytes": len(audio),
                        "rehearsal": result,
                    }
                    if not await db.save_agent_test_voice_result(
                        run.id, workflow.id, context.organization_id, session.id, report
                    ):
                        raise VoiceControlError("voice_report_persistence_failed")
        except asyncio.CancelledError:
            cancelled = True
            error = "voice_job_interrupted"
        except VoiceControlError as exc:
            error = exc.code
        except Exception:  # noqa: BLE001 - redact raw private database/storage errors
            error = "voice_report_persistence_failed"
        try:
            async with asyncio.timeout(15):
                await control.finish(
                    session,
                    context.lease_token,
                    nonce,
                    status="failed" if error else "completed",
                    duration_ms=elapsed_ms,
                    error_code=error,
                    retain_audio=audio_verified and error is not None,
                )
        except asyncio.CancelledError:
            cancelled = True
            error = "voice_settlement_unverified"
        except Exception:  # noqa: BLE001 - an uncertain settlement is never a pass
            error = "voice_settlement_unverified"
    if cancelled:
        raise asyncio.CancelledError
    return {
        "status": "failed" if error else "completed",
        "session_id": session.id,
        "runtime_workflow_run_id": run.id if run else None,
        "error_code": error,
        "rehearsal": result if error is None else None,
        "quality_evaluated": False,
        "cleanup_verified": stopped,
    }


async def read_voice_job_status(db, control, redis, context, signing_secret):
    _, session, _ = await prepare_voice_job(db, control, context)
    response = {
        "status": session.status,
        "session_id": session.id,
        "runtime_workflow_run_id": session.runtime_workflow_run_id,
        "error_code": session.error_code,
        "report_status": "pending",
        "rehearsal": None,
        "quality_evaluated": False,
    }
    if session.status == "completed":
        run = await db.get_workflow_run_by_id(session.runtime_workflow_run_id)
        if run is None or run.workflow_id != session.workflow_id:
            raise VoiceControlError("voice_report_runtime_invalid")
        validate_sandbox_pipeline_binding(
            run, session.workflow_id, run.id, signing_secret
        )
        marker = run.annotations[SANDBOX_ANNOTATION]
        if (
            marker.get("voice_session_id") != session.id
            or marker.get("organization_id") != context.organization_id
            or marker.get("agent_test_run_id") != session.run_id
            or marker.get("scenario_id") != session.scenario_id
        ):
            raise VoiceControlError("voice_report_runtime_invalid")
        report = validate_voice_report(
            run.annotations.get("agent_test_voice_result"),
            session,
            context.organization_id,
        )
        response.update(report_status="verified", rehearsal=report["rehearsal"])
    elif session.status in {"reserved", "running"}:
        from arq.jobs import Job

        async with asyncio.timeout(5):
            job = Job(f"agent-test-voice:{session.id}", redis)
            info = await job.result_info()
        if info is not None:
            result = info.result if info.success else None
            code = result.get("code") if isinstance(result, dict) else None
            # Only stable worker diagnostics can leave the private queue.
            if (
                isinstance(code, str)
                and code.startswith("voice_")
                and len(code) <= 64
                and all(c in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in code)
            ):
                response.update(report_status="blocked", error_code=code)
            else:
                response.update(
                    report_status="blocked", error_code="voice_worker_result_unverified"
                )
    elif session.status in {"failed", "expired", "cancelled"}:
        response["report_status"] = "blocked"
    return response
