"""Tests for VoiceLibraryClient DB operations."""

from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, UTC

import pytest

from api.db.voice_library_client import VoiceLibraryClient


def _make_voice(uuid="v-1", is_public=True, status="ready", user_id=1, org_id=11):
    v = MagicMock()
    v.uuid = uuid
    v.user_id = user_id
    v.organization_id = org_id
    v.name = "Test Voice"
    v.description = None
    v.provider = "dograh_clone"
    v.provider_voice_id = None
    v.language = None
    v.accent = None
    v.gender = None
    v.age = None
    v.use_case = None
    v.is_public = is_public
    v.status = status
    v.audio_preview_url = None
    v.labels = {}
    v.created_at = datetime.now(UTC)
    v.updated_at = datetime.now(UTC)
    return v


@pytest.mark.asyncio
async def test_list_voices_superuser_sees_all_in_org():
    """Superusers see every voice in the org regardless of visibility."""
    client = VoiceLibraryClient()
    voice = _make_voice(is_public=False, status="pending")

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [voice]

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch.object(client, "async_session", return_value=mock_session):
        results = await client.list_voices(organization_id=11, user_id=99, is_superuser=True)

    assert len(results) == 1
    assert mock_session.execute.called


@pytest.mark.asyncio
async def test_list_voices_regular_user_sees_own_voices():
    """Regular users always see their own voices, regardless of status."""
    client = VoiceLibraryClient()
    own_pending = _make_voice(uuid="own", is_public=False, status="pending", user_id=5)

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [own_pending]

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch.object(client, "async_session", return_value=mock_session):
        results = await client.list_voices(organization_id=11, user_id=5, is_superuser=False)

    assert len(results) == 1


@pytest.mark.asyncio
async def test_delete_voice_returns_false_for_other_users_voice():
    """Non-owner, non-superuser cannot delete another user's voice."""
    client = VoiceLibraryClient()
    other_voice = _make_voice(uuid="other", user_id=99, org_id=11)

    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = other_voice

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.delete = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch.object(client, "async_session", return_value=mock_session):
        result = await client.delete_voice(
            voice_uuid="other",
            organization_id=11,
            user_id=1,
            is_superuser=False,
        )

    assert result is False
    mock_session.delete.assert_not_called()


@pytest.mark.asyncio
async def test_delete_voice_superuser_can_delete_any():
    """Superuser can delete any voice in the org."""
    client = VoiceLibraryClient()
    other_voice = _make_voice(uuid="other", user_id=99, org_id=11)

    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = other_voice

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.delete = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch.object(client, "async_session", return_value=mock_session):
        result = await client.delete_voice(
            voice_uuid="other",
            organization_id=11,
            user_id=1,
            is_superuser=True,
        )

    assert result is True
    mock_session.delete.assert_called_once_with(other_voice)
