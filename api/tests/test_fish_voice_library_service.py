from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services.voice_library.fish_service import (
    fetch_fish_catalog,
    fetch_fish_public_voices,
)


class TestFetchFishCatalog:
    @pytest.mark.asyncio
    async def test_fetch_fish_catalog_calls_self_true(self):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "items": [{"_id": "abc123", "title": "My Cloned Voice", "languages": ["en"]}],
            "total": 1,
        }
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            voices = await fetch_fish_catalog("test-key")

        assert voices == [{"_id": "abc123", "title": "My Cloned Voice", "languages": ["en"]}]
        call_kwargs = mock_client.get.call_args.kwargs
        assert call_kwargs["params"]["self"] == "true"
        assert call_kwargs["headers"] == {"Authorization": "Bearer test-key"}


class TestFetchFishPublicVoices:
    @pytest.mark.asyncio
    async def test_fetch_fish_public_voices_forwards_search(self):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"items": [], "total": 0, "has_more": False}
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await fetch_fish_public_voices("test-key", search="Emma", page=2)

        assert result == {"items": [], "total": 0, "has_more": False}
        call_kwargs = mock_client.get.call_args.kwargs
        assert call_kwargs["params"]["title"] == "Emma"
        assert call_kwargs["params"]["page_number"] == 2
        assert "self" not in call_kwargs["params"]
