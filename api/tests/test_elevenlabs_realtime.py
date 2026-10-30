from types import SimpleNamespace
import json
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import LLMFullResponseEndFrame, LLMTextFrame, TTSTextFrame, TTSSpeakFrame, TTSStoppedFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import LLMSettings
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.openai.realtime.events import (
    AudioConfiguration,
    AudioInput,
    AudioOutput,
    InputAudioTranscription,
    SessionProperties,
)

from api.schemas.user_configuration import UserConfiguration
from api.services.configuration.registry import (
    ELEVENLABS_REALTIME_MODELS,
    REGISTRY,
    ServiceType,
)
from api.services.pipecat.realtime.elevenlabs_realtime import (
    DograhElevenLabsRealtimeLLMService,
    _ElevenLabsProtocolSocket,
)
from pipecat.services.openai.realtime.events import parse_server_event
from api.services.pipecat.realtime.openai_realtime import DograhOpenAIRealtimeLLMService


@pytest.mark.parametrize("model", ELEVENLABS_REALTIME_MODELS)
def test_elevenlabs_realtime_configuration_is_registered(model):
    config = UserConfiguration.model_validate(
        {
            "llm": {"provider": "openai", "model": "gpt-4.1", "api_key": "llm-key"},
            "is_realtime": True,
            "realtime": {
                "provider": "elevenlabs_realtime",
                "model": model,
                "voice": "brian",
                "api_key": "el-key",
            },
        }
    )

    assert config.realtime.model == model
    assert config.realtime.voice == "brian"
    assert REGISTRY[ServiceType.REALTIME]["elevenlabs_realtime"]


def test_elevenlabs_realtime_factory_uses_compatible_endpoint_and_voice():
    from api.services.pipecat.service_factory import create_realtime_llm_service

    config = UserConfiguration.model_validate(
        {
            "llm": {"provider": "openai", "model": "gpt-4.1", "api_key": "llm-key"},
            "is_realtime": True,
            "realtime": {
                "provider": "elevenlabs_realtime",
                "model": "eleven_realtime_v1_max",
                "voice": "voice-id-123",
                "language": "es",
                "api_key": "el-key",
            },
        }
    )

    with patch(
        "api.services.pipecat.realtime.elevenlabs_realtime.DograhElevenLabsRealtimeLLMService"
    ) as service_cls:
        service_cls.Settings = DograhElevenLabsRealtimeLLMService.Settings
        result = create_realtime_llm_service(config, audio_config=None)

    assert result is service_cls.return_value
    kwargs = service_cls.call_args.kwargs
    assert kwargs["api_key"] == "el-key"
    assert kwargs["base_url"] == "wss://api.elevenlabs.io/v1/convai/realtime"
    assert kwargs["settings"].model == "eleven_realtime_v1_max"
    assert kwargs["settings"].session_properties.audio.output.voice == "voice-id-123"
    assert kwargs["settings"].session_properties.audio.input.transcription.language == "es"


@pytest.mark.parametrize("realtime_voice,expected_voice", [
    ("brian", "voice-engine-id"),
    ("selected-realtime-id", "selected-realtime-id"),
])
def test_elevenlabs_realtime_voice_selection_preserves_legacy_fallback(realtime_voice, expected_voice):
    from api.services.pipecat.service_factory import create_realtime_llm_service

    config = UserConfiguration.model_validate(
        {
            "llm": {"provider": "openai", "model": "gpt-4.1", "api_key": "llm-key"},
            "tts": {
                "provider": "elevenlabs",
                "model": "eleven_v4",
                "voice": "voice-engine-id",
                "api_key": "tts-key",
            },
            "is_realtime": True,
            "realtime": {
                "provider": "elevenlabs_realtime",
                "model": "eleven_realtime_v1_max",
                "voice": realtime_voice,
                "api_key": "el-key",
            },
        }
    )

    with patch(
        "api.services.pipecat.realtime.elevenlabs_realtime.DograhElevenLabsRealtimeLLMService"
    ) as service_cls:
        service_cls.Settings = DograhElevenLabsRealtimeLLMService.Settings
        create_realtime_llm_service(config, audio_config=None)

    assert (
        service_cls.call_args.kwargs["settings"].session_properties.audio.output.voice
        == expected_voice
    )


def test_client_secret_config_uses_pcm24k_model_and_selected_voice():
    service = DograhElevenLabsRealtimeLLMService(
        api_key="secret-never-assert-or-log",
        base_url="wss://api.elevenlabs.io/v1/convai/realtime",
        settings=DograhElevenLabsRealtimeLLMService.Settings(
            model="eleven_realtime_v1_mini",
            session_properties=SessionProperties(
                audio=AudioConfiguration(
                    input=AudioInput(transcription=InputAudioTranscription()),
                    output=AudioOutput(voice="brian"),
                ),
            ),
        ),
    )

    session = service._client_secret_session()

    assert session["type"] == "realtime"
    assert session["model"] == "eleven_realtime_v1_mini"
    assert session["output_modalities"] == ["audio"]
    assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert service._settings.session_properties.audio.input.transcription is None
    assert session["audio"]["output"]["format"] == {"type": "audio/pcm"}
    assert session["audio"]["output"]["voice"] == "brian"


def test_client_secret_keeps_selected_transcription_language_without_foreign_model():
    service = DograhElevenLabsRealtimeLLMService(
        api_key="synthetic-test-key",
        settings=DograhElevenLabsRealtimeLLMService.Settings(
            model="eleven_realtime_v1_mini",
            session_properties=SessionProperties(
                audio=AudioConfiguration(
                    input=AudioInput(transcription=InputAudioTranscription(language="es")),
                    output=AudioOutput(voice="brian"),
                ),
            ),
        ),
    )

    session = service._client_secret_session()

    assert session["audio"]["input"]["transcription"] == {"language": "es"}
    assert service._settings.session_properties.audio.input.transcription is None


@pytest.mark.asyncio
async def test_session_ready_alone_does_not_start_an_unconfigured_opening():
    service = DograhElevenLabsRealtimeLLMService(
        api_key="test-key",
        settings=DograhElevenLabsRealtimeLLMService.Settings(
            model="eleven_realtime_v1_mini",
            session_properties=SessionProperties(),
        ),
    )
    context = LLMContext()
    service._context = context

    with patch(
        "api.services.pipecat.realtime.openai_realtime.OpenAIRealtimeLLMService._create_response",
        new=AsyncMock(),
    ) as create_response:
        await service._handle_evt_session_updated(object())

    create_response.assert_not_awaited()
    assert service._handled_initial_context is False
    assert service._context is context
    assert service._initial_response_requested is False


@pytest.mark.asyncio
async def test_session_ready_does_not_duplicate_a_deferred_initial_response():
    service = DograhElevenLabsRealtimeLLMService(
        api_key="test-key",
        settings=DograhElevenLabsRealtimeLLMService.Settings(
            model="eleven_realtime_v1_mini",
            session_properties=SessionProperties(),
        ),
    )
    service._context = LLMContext()
    service.send_client_event = AsyncMock()

    with patch(
        "api.services.pipecat.realtime.openai_realtime.OpenAIRealtimeLLMService._create_response",
        new=AsyncMock(),
    ) as create_response:
        await service.process_frame(TTSSpeakFrame("Hello, welcome."), FrameDirection.DOWNSTREAM)
        service.send_client_event.assert_not_awaited()
        assert service._initial_response_requested is False
        await service._handle_evt_session_updated(object())
        await service._handle_evt_session_updated(object())

    create_response.assert_awaited_once_with()
    service.send_client_event.assert_awaited_once()
    assert "Hello, welcome." in service.send_client_event.call_args.args[0].item.content[0].text


@pytest.mark.asyncio
async def test_authored_greeting_is_sent_as_input_without_polluting_workflow_context():
    service = DograhElevenLabsRealtimeLLMService(
        api_key="test-key",
        settings=DograhElevenLabsRealtimeLLMService.Settings(
            model="eleven_realtime_v1_mini", session_properties=SessionProperties(),
        ),
    )
    service._context = LLMContext()
    service._api_session_ready = True
    service.send_client_event = AsyncMock()
    greeting = "Hi, this is Sam from Sysevo — how can I help?"

    with patch(
        "api.services.pipecat.realtime.openai_realtime.OpenAIRealtimeLLMService._create_response",
        new=AsyncMock(),
    ) as create_response:
        await service.process_frame(TTSSpeakFrame(greeting), FrameDirection.DOWNSTREAM)

    item = service.send_client_event.call_args.args[0].item
    assert item.role == "user"
    assert greeting in item.content[0].text
    assert item.id in service._messages_added_manually
    assert service._context.messages == []
    assert service._pending_opening_text is None
    create_response.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_caller_message", [False, True])
async def test_initial_context_only_adds_opening_input_when_no_caller_input_exists(has_caller_message):
    service = DograhElevenLabsRealtimeLLMService(
        api_key="test-key",
        settings=DograhElevenLabsRealtimeLLMService.Settings(
            model="eleven_realtime_v1_mini", session_properties=SessionProperties(),
        ),
    )
    messages = [{"role": "user", "content": "Hello"}] if has_caller_message else []
    context = LLMContext(messages.copy())
    service._api_session_ready = True
    service.send_client_event = AsyncMock()
    with patch(
        "api.services.pipecat.realtime.openai_realtime.OpenAIRealtimeLLMService._create_response",
        new=AsyncMock(),
    ) as create_response:
        await service._handle_context(context)

    assert service.send_client_event.await_count == (0 if has_caller_message else 1)
    assert context.messages == messages
    create_response.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_workflow_context_replaces_realtime_services_empty_context():
    from api.services.workflow.pipecat_engine import PipecatEngine

    engine = object.__new__(PipecatEngine)
    live_context = LLMContext()
    engine.context = live_context
    engine.llm = SimpleNamespace(
        _context=LLMContext(),
        _update_settings=AsyncMock(),
    )

    await engine._update_llm_context("Agent instructions", [])

    assert engine.llm._context is live_context
    engine.llm._update_settings.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type,status", [("response.created", "in_progress"), ("response.done", "completed")])
async def test_elevenlabs_response_without_nullable_status_details_is_parseable(event_type, status):
    payload = {"type": event_type, "event_id": "evt-test", "response": {
        "id": "resp-test", "object": "realtime.response", "status": status,
        "output": [], "usage": None, "output_modalities": ["audio"],
    }}
    raw = json.dumps(payload)

    class Socket:
        async def __aiter__(self):
            yield raw

        send = AsyncMock()
        close = AsyncMock()

    socket = Socket()
    compatible = _ElevenLabsProtocolSocket(socket)
    normalized = [message async for message in compatible]
    parsed = parse_server_event(normalized[0])
    assert parsed.response.status_details is None
    assert parsed.response.status == status
    await compatible.send("test")
    socket.send.assert_awaited_once_with("test")
    await compatible.close()
    socket.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_protocol_socket_preserves_audio_payload_without_reencoding():
    raw = '{"type":"response.output_audio.delta","delta":"AAECAw=="}'

    class Socket:
        async def __aiter__(self):
            yield raw

    assert [message async for message in _ElevenLabsProtocolSocket(Socket())] == [raw]


@pytest.mark.asyncio
async def test_response_without_token_usage_finishes_audio_and_allows_the_next_turn():
    service = DograhElevenLabsRealtimeLLMService(
        api_key="test-key",
        settings=DograhElevenLabsRealtimeLLMService.Settings(
            model="eleven_realtime_v1_mini", session_properties=SessionProperties(),
        ),
    )
    service.push_frame = AsyncMock()
    service.stop_processing_metrics = AsyncMock()
    service.start_llm_usage_metrics = AsyncMock()
    service._current_audio_response = object()
    service._current_assistant_response = object()

    await service._handle_evt_response_done(SimpleNamespace(response=SimpleNamespace(
        status="completed", usage=None, output=[],
    )))

    assert isinstance(service.push_frame.call_args_list[0].args[0], TTSStoppedFrame)
    assert isinstance(service.push_frame.call_args_list[1].args[0], LLMFullResponseEndFrame)
    assert service._current_assistant_response is None
    service.stop_processing_metrics.assert_awaited_once()
    service.start_llm_usage_metrics.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_buffer_clear_notification_does_not_abort_websocket_audio():
    audio = '{"type":"response.output_audio.delta","delta":"AAECAw=="}'

    class Socket:
        async def __aiter__(self):
            yield '{"type":"output_audio_buffer.cleared","response_id":"resp-test"}'
            yield audio

    assert [message async for message in _ElevenLabsProtocolSocket(Socket())] == [audio]


@pytest.mark.asyncio
async def test_workflow_instruction_change_is_sent_once_on_next_response_without_caller_transcript():
    service = DograhElevenLabsRealtimeLLMService(
        api_key="test-key",
        settings=DograhElevenLabsRealtimeLLMService.Settings(
            model="eleven_realtime_v1_max", session_properties=SessionProperties(),
        ),
    )
    service._context = LLMContext()
    service.send_client_event = AsyncMock()
    await service._update_settings(LLMSettings(system_instruction="Opening node"))
    assert service._pending_workflow_instruction is None
    service._handled_initial_context = True
    service._api_session_ready = True
    await service._update_settings(LLMSettings(system_instruction="Ask which day to book."))
    assert service._pending_workflow_instruction == "Ask which day to book."
    service.send_client_event.reset_mock()
    with patch(
        "api.services.pipecat.realtime.openai_realtime.OpenAIRealtimeLLMService._create_response",
        new=AsyncMock(),
    ) as create_response:
        await service._create_response()
        await service._create_response()
    item = service.send_client_event.call_args.args[0].item
    assert "Ask which day to book." in item.content[0].text
    assert item.id in service._messages_added_manually
    assert service._context.messages == []
    assert service._pending_workflow_instruction is None
    service.send_client_event.assert_awaited_once()
    assert create_response.await_count == 2

@pytest.mark.asyncio
async def test_caller_transcript_uses_speech_start_not_late_asr_completion():
    service = DograhElevenLabsRealtimeLLMService(api_key="test-key")
    service.broadcast_frame = AsyncMock()
    service._call_event_handler = AsyncMock()
    service._handle_user_transcription = AsyncMock()
    event = SimpleNamespace(item_id="caller-1", transcript="What do you do?")
    with patch("api.services.pipecat.realtime.elevenlabs_realtime.time_now_iso8601", return_value="2026-10-23T04:00:01Z"), patch.object(
        DograhOpenAIRealtimeLLMService, "_handle_evt_speech_started", new=AsyncMock()
    ):
        await service._handle_evt_speech_started(SimpleNamespace(item_id="caller-1"))
    with patch("api.services.pipecat.realtime.elevenlabs_realtime.time_now_iso8601", return_value="2026-10-23T04:00:09Z"):
        await service.handle_evt_input_audio_transcription_completed(event)
    assert service.broadcast_frame.call_args.kwargs["timestamp"] == "2026-10-23T04:00:01Z"
    assert service.broadcast_frame.call_args.kwargs["result"] is event
    assert service.broadcast_frame.call_args.kwargs["finalized"] is True


@pytest.mark.asyncio
async def test_assistant_deltas_keep_item_identity_timestamp_and_original_spacing():
    service = DograhElevenLabsRealtimeLLMService(api_key="test-key")
    service.push_frame = AsyncMock()
    for delta in ["book", "ing?"]:
        await service._handle_evt_audio_transcript_delta(SimpleNamespace(item_id="assistant-1", delta=delta))
    frames = [args.args[0] for args in service.push_frame.call_args_list]
    text_frames = [frame for frame in frames if isinstance(frame, TTSTextFrame)]
    assert [frame.text for frame in text_frames] == ["book", "ing?"]
    assert all(frame.transcript_item_id == "assistant-1" for frame in text_frames)
    assert text_frames[0].transcript_timestamp == text_frames[1].transcript_timestamp
    assert all(frame.includes_inter_frame_spaces for frame in text_frames)
    llm_frames = [frame for frame in frames if isinstance(frame, LLMTextFrame)]
    assert len(llm_frames) == 2
    assert all(frame.append_to_context is False for frame in llm_frames)


@pytest.mark.asyncio
async def test_caller_key_presses_are_sent_as_input_and_answered():
    """Keypad & Native Phone Tools: a caller pressing 1 reaches the realtime model."""
    from pipecat.frames.frames import LLMMessagesAppendFrame

    service = DograhElevenLabsRealtimeLLMService(
        api_key="test-key",
        settings=DograhElevenLabsRealtimeLLMService.Settings(
            model="eleven_realtime_v1_mini", session_properties=SessionProperties(),
        ),
    )
    service._context = LLMContext()
    service._handled_initial_context = True
    service._api_session_ready = True
    service.send_client_event = AsyncMock()
    service.push_frame = AsyncMock()
    service.start_processing_metrics = AsyncMock()
    service.start_ttfb_metrics = AsyncMock()

    await service.process_frame(
        LLMMessagesAppendFrame(
            [{"role": "user", "content": "[The caller pressed these keys on their phone keypad: 1]"}],
            run_llm=True,
        ),
        FrameDirection.DOWNSTREAM,
    )

    sent = [call.args[0] for call in service.send_client_event.await_args_list]
    assert sent[0].type == "conversation.item.create"
    assert "keypad: 1" in sent[0].item.content[0].text
    assert sent[1].type == "response.create"
