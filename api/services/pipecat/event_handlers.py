import asyncio

from loguru import logger

from api.db import db_client
from api.enums import PostHogEvent, WorkflowRunState
from api.services.campaign.circuit_breaker import circuit_breaker
from api.services.integrations import IntegrationRuntimeSession
from api.services.pipecat.audio_config import AudioConfig
# Max time the greeting will wait on an in-flight pre-call fetch before starting
# anyway. Keeps a slow memory service from stalling the agent on pickup.
_PRE_CALL_GREETING_WAIT_S = 1.5
from api.services.pipecat.in_memory_buffers import (
    InMemoryAudioBuffer,
    InMemoryLogsBuffer,
)
from api.services.pipecat.llm_error_classification import classify_llm_exhaustion
from api.services.pipecat.pipeline_metrics_aggregator import PipelineMetricsAggregator
from api.services.pipecat.tracing_config import get_trace_url
from api.services.posthog_client import capture_event
from api.services.workflow.pipecat_engine import PipecatEngine
from api.services.workflow.variable_resolution import (
    fill_if_absent,
    remap_memory_variables,
)
from api.tasks.arq import enqueue_job
from api.tasks.function_names import FunctionNames
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
)
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.utils.enums import EndTaskReason


async def _capture_call_event(
    workflow_run_id: int,
    user_provider_id: str | None,
    event: str,
    extra_properties: dict | None = None,
) -> None:
    """Look up workflow_run for call metadata and fire a PostHog event.
    Meant to be run via asyncio.create_task() so it never blocks the pipeline."""
    try:
        workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
        properties = {
            "workflow_run_id": workflow_run_id,
            "workflow_id": workflow_run.workflow_id if workflow_run else None,
            "call_type": workflow_run.mode if workflow_run else None,
            "call_direction": (workflow_run.initial_context or {}).get(
                "direction", "outbound"
            )
            if workflow_run
            else None,
        }
        if extra_properties:
            properties.update(extra_properties)
        capture_event(
            distinct_id=user_provider_id,
            event=event,
            properties=properties,
        )
    except Exception:
        logger.exception(f"Background PostHog capture failed for '{event}'")


def register_event_handlers(
    task: PipelineTask,
    transport,
    workflow_run_id: int,
    engine: PipecatEngine,
    audio_buffer: AudioBufferProcessor,
    in_memory_logs_buffer: InMemoryLogsBuffer,
    pipeline_metrics_aggregator: PipelineMetricsAggregator,
    audio_config=AudioConfig,
    pre_call_fetch_task: asyncio.Task | None = None,
    pre_call_fetch_is_memory: bool = False,
    memory_attr_map: dict[str, str] | None = None,
    user_provider_id: str | None = None,
    integration_runtime_sessions: list[IntegrationRuntimeSession] | None = None,
):
    """Register all event handlers for transport and task events.

    Returns:
        in_memory_audio_buffer for use by other handlers.
    """
    # Initialize in-memory buffers with proper audio configuration
    sample_rate = audio_config.pipeline_sample_rate if audio_config else 16000
    num_channels = 1  # Pipeline audio is always mono

    logger.debug(
        f"Initializing audio buffer for workflow {workflow_run_id} "
        f"with sample_rate={sample_rate}Hz, channels={num_channels}"
    )

    in_memory_audio_buffer = InMemoryAudioBuffer(
        workflow_run_id=workflow_run_id,
        sample_rate=sample_rate,
        num_channels=num_channels,
    )
    # Track both events to ensure the initial response is only triggered after both occur
    ready_state = {
        "pipeline_started": False,
        "client_connected": False,
        "initial_response_triggered": False,
    }

    async def maybe_trigger_initial_response():
        """Start the conversation after both pipeline_started and client_connected events.

        If a pre-call fetch is in progress, plays a ringer while waiting for the
        response, then merges the result into the call context before proceeding.
        """
        if (
            ready_state["pipeline_started"]
            and ready_state["client_connected"]
            and not ready_state["initial_response_triggered"]
        ):
            ready_state["initial_response_triggered"] = True

            asyncio.create_task(
                _capture_call_event(
                    workflow_run_id, user_provider_id, PostHogEvent.CALL_STARTED
                )
            )

            # Wait (briefly) for the pre-call fetch if it's still in flight.
            # We deliberately do NOT play an audible ringer here: on an answered
            # call — especially outbound — a ring tone after pickup is confusing
            # and was heard as the "ringing sound effect" that delayed the agent.
            # Cap the wait so a slow memory service can never stall the greeting;
            # if it hasn't returned in time, start the greeting now (the shielded
            # task keeps running, and outbound memory lookups add nothing anyway).
            if pre_call_fetch_task is not None:
                fetch_result = None
                if pre_call_fetch_task.done():
                    fetch_result = pre_call_fetch_task.result()
                else:
                    try:
                        fetch_result = await asyncio.wait_for(
                            asyncio.shield(pre_call_fetch_task),
                            timeout=_PRE_CALL_GREETING_WAIT_S,
                        )
                    except asyncio.TimeoutError:
                        logger.info(
                            f"Pre-call fetch slow (>{_PRE_CALL_GREETING_WAIT_S:.1f}s); "
                            "starting greeting without blocking on it"
                        )

                if fetch_result:
                    if pre_call_fetch_is_memory:
                        # Land memory values on the variables they're bound to
                        # (a variable may map to a differently-named memory
                        # attribute), then fill only the gaps — campaign/explicit
                        # values and non-empty workflow defaults win.
                        fetch_result = remap_memory_variables(
                            fetch_result, memory_attr_map or {}
                        )
                        fill_if_absent(engine._call_context_vars, fetch_result)
                    else:
                        # Generic pre-call HTTP fetch keeps its enrich/override
                        # behaviour.
                        engine._call_context_vars.update(fetch_result)
                    try:
                        await db_client.update_workflow_run(
                            workflow_run_id,
                            initial_context={**engine._call_context_vars},
                        )
                    except Exception as e:
                        logger.error(f"Failed to persist pre-call fetch context: {e}")
                    logger.info(
                        f"Pre-call fetch complete, merged keys: "
                        f"{list(fetch_result.keys())}"
                    )

            # Set the start node now (after pre-call fetch data is merged)
            # so that render_template() has the complete _call_context_vars.
            await engine.set_node(engine.workflow.start_node_id)
            await engine.queue_node_opening(
                node_id=engine.workflow.start_node_id,
                previous_node_id=None,
                generate_if_no_greeting=True,
            )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport, _participant):
        logger.debug("In on_client_connected callback handler")
        await audio_buffer.start_recording()
        ready_state["client_connected"] = True
        await maybe_trigger_initial_response()

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _participant):
        call_disposed = engine.is_call_disposed()

        logger.info(
            f"In on_client_disconnected callback handler for run {workflow_run_id}. "
            f"Call disposed: {call_disposed}"
        )

        # Stop recordings — fires on_audio_data as background task with accumulated audio
        logger.debug(f"Calling audio_buffer.stop_recording() in on_client_disconnected for run {workflow_run_id}")
        await audio_buffer.stop_recording()

        await engine.end_call_with_reason(
            EndTaskReason.USER_HANGUP.value, abort_immediately=True
        )

    @task.event_handler("on_pipeline_started")
    async def on_pipeline_started(_task: PipelineTask, _frame: Frame):
        logger.debug("In on_pipeline_started callback handler")
        ready_state["pipeline_started"] = True
        await maybe_trigger_initial_response()

    @task.event_handler("on_pipeline_error")
    async def on_pipeline_error(_task: PipelineTask, frame: Frame):
        logger.warning(f"Pipeline error for workflow run {workflow_run_id}: {frame}")

        # Distinguish an LLM rate-limit/quota-exhaustion failure from a
        # generic pipeline error: same graceful end-call path either way,
        # but this failure mode is silent otherwise (the call still "connects"
        # from the telephony side, so nothing else flags it) and it's the
        # known recurring "agent rang but no audio" cause when a provider key
        # runs out of credits — worth a loud, distinct signal so an operator
        # finds out from a log/campaign-log entry instead of a client report.
        llm_exhaustion = classify_llm_exhaustion(
            frame.exception if isinstance(frame, ErrorFrame) else None
        )

        try:
            workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
            if workflow_run and workflow_run.campaign_id:
                await circuit_breaker.record_and_evaluate(
                    campaign_id=workflow_run.campaign_id,
                    is_failure=True,
                    workflow_run_id=workflow_run_id,
                    reason="pipeline_error",
                )
                if llm_exhaustion:
                    await db_client.append_campaign_log(
                        campaign_id=workflow_run.campaign_id,
                        level="error",
                        event="llm_provider_exhausted",
                        message=(
                            f"Call ended: LLM provider returned a rate-limit/"
                            f"quota error ({llm_exhaustion['exception_type']}) — "
                            "check the provider's credits/quota."
                        ),
                        details={
                            "workflow_run_id": workflow_run_id,
                            "workflow_id": workflow_run.workflow_id,
                            **llm_exhaustion,
                        },
                    )
            asyncio.create_task(
                _capture_call_event(
                    workflow_run_id,
                    user_provider_id,
                    PostHogEvent.CALL_FAILED,
                    extra_properties={
                        "error_reason": "llm_provider_exhausted"
                        if llm_exhaustion
                        else "pipeline_error",
                    },
                )
            )
        except Exception as e:
            logger.error(f"Error recording circuit breaker failure: {e}", exc_info=True)

        if llm_exhaustion:
            organization_id = await engine._get_organization_id()
            logger.error(
                f"[LLM_PROVIDER_EXHAUSTED] workflow_run={workflow_run_id} "
                f"organization_id={organization_id}: {llm_exhaustion['exception_message']}"
            )

        await engine.end_call_with_reason(
            EndTaskReason.PIPELINE_ERROR.value,
            abort_immediately=True,
            extra_context={"llm_provider_exhausted": llm_exhaustion}
            if llm_exhaustion
            else None,
        )

    @task.event_handler("on_pipeline_finished")
    async def on_pipeline_finished(
        task: PipelineTask,
        _frame: Frame,
    ):
        logger.info(f"In on_pipeline_finished callback handler for run {workflow_run_id}")

        workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)

        # Stop recordings (no-op if already stopped by on_client_disconnected or process_frame)
        logger.debug(f"Calling audio_buffer.stop_recording() in on_pipeline_finished for run {workflow_run_id}")
        await audio_buffer.stop_recording()

        gathered_context = await engine.get_gathered_context()

        # Add trace URL if available (must be done before conversation tracing ends)
        if task.turn_trace_observer:
            trace_id = task.turn_trace_observer.get_trace_id()
            if trace_id:
                trace_url = get_trace_url(trace_id)
                if trace_url:
                    gathered_context["trace_url"] = trace_url
                    logger.debug(f"Added trace URL to gathered_context: {trace_url}")

        # also consider existing gathered context in workflow_run
        gathered_context = {**workflow_run.gathered_context, **gathered_context}

        # Set user_speech call tag
        call_tags = gathered_context.get("call_tags", [])

        try:
            has_user_speech = in_memory_logs_buffer.contains_user_speech()
        except Exception:
            has_user_speech = False

        if has_user_speech and "user_speech" not in call_tags:
            call_tags.append("user_speech")

        # Append any keys from gathered_context that start with 'tag_' to call_tags
        for key in gathered_context:
            if key.startswith("tag_") and key not in call_tags:
                call_tags.append(gathered_context[key])

        gathered_context["call_tags"] = call_tags

        # Store disposition code in workflow for dynamic filtering
        disposition_code = gathered_context.get("mapped_call_disposition")
        if disposition_code and workflow_run:
            try:
                await db_client.add_call_disposition_code(
                    workflow_run.workflow_id, disposition_code
                )
            except Exception as e:
                logger.error(
                    f"Error storing disposition code in workflow: {e}",
                    exc_info=True,
                )

        # Clean up engine resources (including voicemail detector)
        integration_logs: dict[str, object] = {}
        for runtime_session in integration_runtime_sessions or []:
            try:
                session_logs = await runtime_session.on_call_finished(
                    gathered_context=gathered_context
                )
                if session_logs:
                    integration_logs.update(session_logs)
            except Exception as e:
                logger.error(
                    f"Error finalizing integration runtime session '{runtime_session.name}': {e}",
                    exc_info=True,
                )

        await engine.cleanup()

        # ------------------------------------------------------------------
        # Close Smart-Turn WebSocket if the transport's analyzer supports it
        # ------------------------------------------------------------------
        try:
            turn_analyzer = None

            # Most transports store their params (with turn_analyzer) directly.
            if hasattr(transport, "_params") and transport._params:
                turn_analyzer = getattr(transport._params, "turn_analyzer", None)

            # Fallback: some transports expose params through input() instance.
            if turn_analyzer is None and hasattr(transport, "input"):
                try:
                    input_transport = transport.input()
                    if input_transport and hasattr(input_transport, "_params"):
                        turn_analyzer = getattr(
                            input_transport._params, "turn_analyzer", None
                        )
                except Exception:
                    pass

            if turn_analyzer and hasattr(turn_analyzer, "close"):
                await turn_analyzer.close()
                logger.debug("Closed turn analyzer websocket")
        except Exception as exc:
            logger.warning(f"Failed to close Smart-Turn analyzer gracefully: {exc}")

        usage_info = pipeline_metrics_aggregator.get_all_usage_metrics_serialized()

        logger.debug(
            f"Usage metrics: {usage_info}, Gathered context: {gathered_context}"
        )

        await db_client.update_workflow_run(
            run_id=workflow_run_id,
            usage_info=usage_info,
            gathered_context=gathered_context,
            is_completed=True,
            state=WorkflowRunState.COMPLETED.value,
        )

        asyncio.create_task(
            _capture_call_event(
                workflow_run_id, user_provider_id, PostHogEvent.CALL_COMPLETED
            )
        )

        logs_update: dict[str, object] = {}
        if not in_memory_logs_buffer.is_empty:
            try:
                feedback_events = in_memory_logs_buffer.get_events()
                logs_update["realtime_feedback_events"] = feedback_events
                logger.debug(
                    f"Saved {len(feedback_events)} feedback events to workflow run logs"
                )
            except Exception as e:
                logger.error(f"Error saving realtime feedback logs: {e}", exc_info=True)
        else:
            logger.debug("Logs buffer is empty, skipping save")

        logs_update.update(integration_logs)

        if logs_update:
            try:
                await db_client.update_workflow_run(
                    run_id=workflow_run_id,
                    logs=logs_update,
                )
            except Exception as e:
                logger.error(f"Error saving workflow run logs: {e}", exc_info=True)

        # Write buffers to temp files and enqueue combined processing task
        audio_temp_path = None
        transcript_temp_path = None

        # Yield the event loop so any pending on_audio_data background tasks (fired
        # by stop_recording() or intermediate buffer flushes) have a chance to
        # complete and append their chunks to in_memory_audio_buffer before we check.
        await asyncio.sleep(0)

        try:
            logger.info(
                f"Audio buffer size for run {workflow_run_id}: "
                f"{in_memory_audio_buffer.size} bytes, empty={in_memory_audio_buffer.is_empty}"
            )
            if not in_memory_audio_buffer.is_empty:
                audio_temp_path = await in_memory_audio_buffer.write_to_temp_file()
            else:
                logger.warning(
                    f"Audio buffer is empty for run {workflow_run_id}, skipping recording upload"
                )

            transcript_temp_path = in_memory_logs_buffer.write_transcript_to_temp_file()
            if not transcript_temp_path:
                logger.debug("No transcript events in logs buffer, skipping upload")

        except Exception as e:
            logger.error(f"Error preparing buffers for S3 upload: {e}", exc_info=True)

        # Combined task: uploads artifacts, runs integrations (including QA),
        # then calculates cost (so QA token usage is captured in usage_info)
        await enqueue_job(
            FunctionNames.PROCESS_WORKFLOW_COMPLETION,
            workflow_run_id,
            audio_temp_path,
            transcript_temp_path,
        )

    # Return the buffer so it can be passed to other handlers
    return in_memory_audio_buffer


def register_audio_data_handler(
    audio_buffer: AudioBufferProcessor,
    workflow_run_id,
    in_memory_buffer: InMemoryAudioBuffer,
):
    """Register event handler for audio data"""
    logger.info(f"Registering audio data handler for workflow run {workflow_run_id}")

    @audio_buffer.event_handler("on_audio_data")
    async def on_audio_data(buffer, audio, sample_rate, num_channels):
        if not audio:
            logger.debug(f"on_audio_data fired with empty audio for run {workflow_run_id}, skipping")
            return

        # Use in-memory buffer
        try:
            await in_memory_buffer.append(audio)
            logger.debug(
                f"Appended {len(audio)} bytes to in-memory audio buffer for run {workflow_run_id} "
                f"(total: {in_memory_buffer.size} bytes)"
            )
        except MemoryError as e:
            logger.error(f"Memory buffer full: {e}")
