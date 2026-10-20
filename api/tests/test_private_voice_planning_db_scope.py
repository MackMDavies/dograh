"""Execute the private-planning DAL predicate against real in-memory SQL rows.

SQLite covers this AND/OR ownership predicate; this does not certify production
Postgres or provider access. No external database or credentials are used.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import Session

from api.db.models import VoiceLibraryModel
from api.db.voice_library_client import VoiceLibraryClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "voice_uuid,visible",
    [
        ("own-ready", True),
        ("own-pending", True),
        ("foreign-private", False),
        ("foreign-public-pending", False),
        ("foreign-public-ready", True),
        ("absent", False),
    ],
)
async def test_actual_dal_only_returns_org_voices_or_explicit_ready_public_voices(
    voice_uuid, visible
):
    engine = create_engine("sqlite://")
    try:
        VoiceLibraryModel.__table__.create(engine)
        rows = []
        for identity, org, public, status in [
            ("own-ready", 42, False, "ready"),
            ("own-pending", 42, False, "pending"),
            ("foreign-private", 99, False, "ready"),
            ("foreign-public-pending", 99, True, "pending"),
            ("foreign-public-ready", 99, True, "ready"),
        ]:
            rows.append(
                {
                    "uuid": identity,
                    "organization_id": org,
                    "user_id": 7,
                    "name": "QA voice",
                    "provider": "openai",
                    "provider_voice_id": "alloy",
                    "is_public": public,
                    "status": status,
                }
            )
        with Session(engine) as sql:
            sql.execute(insert(VoiceLibraryModel), rows)
            sql.commit()

            async def execute(query):
                return sql.execute(query)

            session = SimpleNamespace(execute=AsyncMock(side_effect=execute))
            context = AsyncMock()
            context.__aenter__.return_value = session
            db = SimpleNamespace(async_session=Mock(return_value=context))
            result = await VoiceLibraryClient.get_voice_for_private_planning(
                db, voice_uuid, 42
            )
            assert (result is not None) is visible
            if visible:
                assert result.uuid == voice_uuid
            session.execute.assert_awaited_once()
    finally:
        engine.dispose()
