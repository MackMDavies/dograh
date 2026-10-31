from types import SimpleNamespace
from unittest.mock import patch

from pipecat.transcriptions.language import Language

from api.services.pipecat.service_factory import create_stt_service


def test_flux_multilingual_service_receives_configured_language_hints():
    user_config = SimpleNamespace(
        stt=SimpleNamespace(
            provider="deepgram",
            model="flux-general-multi",
            api_key="test-key",
            language="es,en",
        )
    )
    audio_config = SimpleNamespace(transport_in_sample_rate=16000)

    with patch("api.services.pipecat.service_factory.DeepgramFluxSTTService") as service:
        create_stt_service(user_config, audio_config)

    settings = service.call_args.kwargs["settings"]
    assert settings.language_hints == [Language.ES, Language.EN]
