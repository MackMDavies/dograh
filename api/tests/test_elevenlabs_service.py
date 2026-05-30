import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_clone_voice_calls_elevenlabs_api():
    from api.services.voice_library.elevenlabs_service import clone_voice_with_elevenlabs

    mock_response = MagicMock()
    mock_response.json.return_value = {"voice_id": "el-abc123"}
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("api.services.voice_library.elevenlabs_service.httpx.AsyncClient", return_value=mock_client):
        result = await clone_voice_with_elevenlabs("fake-api-key", "Test Voice", b"audio-data")

    assert result["voice_id"] == "el-abc123"
    mock_client.post.assert_called_once()
    call_kwargs = mock_client.post.call_args
    assert "xi-api-key" in call_kwargs.kwargs["headers"]


@pytest.mark.asyncio
async def test_fetch_elevenlabs_catalog_returns_voices():
    from api.services.voice_library.elevenlabs_service import fetch_elevenlabs_catalog

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "voices": [{"voice_id": "v1", "name": "Rachel", "preview_url": "http://ex.com/p.mp3"}]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("api.services.voice_library.elevenlabs_service.httpx.AsyncClient", return_value=mock_client):
        result = await fetch_elevenlabs_catalog("fake-api-key")

    assert len(result) == 1
    assert result[0]["voice_id"] == "v1"
