"""Release compatibility with the phone backend already running in production."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from api.db.models import (
    PlatformDialerTwilioCredentialsModel,
    PlatformTwilioCredentialsModel,
    PlatformTwilioDialerCredentialsModel,
    PlatformSignalWireDialerCredentialsModel,
)
from api.db.platform_settings_client import PlatformSettingsClient


def client_with_row(row):
    client = object.__new__(PlatformSettingsClient)
    session = AsyncMock()
    session.add = MagicMock()
    session.get.return_value = row
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    client.async_session = MagicMock(return_value=context)
    return client, session


def test_dialer_names_share_the_deployed_singleton():
    assert PlatformDialerTwilioCredentialsModel is PlatformTwilioDialerCredentialsModel
    assert PlatformTwilioDialerCredentialsModel.__tablename__ == "platform_twilio_dialer_credentials"
    assert "dialer_api_key_sid" not in PlatformTwilioCredentialsModel.__table__.columns
    assert "dialer_api_key_secret_encrypted" not in PlatformTwilioCredentialsModel.__table__.columns
    assert "dialer_twiml_app_sid" not in PlatformTwilioCredentialsModel.__table__.columns
    assert "dialer_default_caller_id" not in PlatformTwilioCredentialsModel.__table__.columns


def test_both_applied_histories_have_one_upgrade_head():
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["dv20261025a"]
    live = [step.revision.revision for step in scripts._upgrade_revs("heads", "e26a1015c001")]
    canonical = [step.revision.revision for step in scripts._upgrade_revs("heads", "ef20261016")]
    assert "ef20261016" in live
    assert "e26a1015c001" in canonical
    assert "vr20261026a" in live and "vr20261026a" in canonical
    assert "af741b6799bd" not in live  # Never replay the applied credential copy.


@pytest.mark.asyncio
@pytest.mark.parametrize("keyword_call", [False, True])
async def test_vonage_accepts_live_dictionary_and_canonical_keywords(keyword_call):
    client, session = client_with_row(None)
    credentials = dict(application_id="app", api_key="key", api_secret="secret",
                       private_key="private", signature_secret="signature")
    if keyword_call:
        await client.save_platform_vonage_dialer_credentials(**credentials)
    else:
        await client.save_platform_vonage_dialer_credentials(credentials)
    assert session.add.call_args.args[0].credentials == credentials
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_canonical_twilio_name_delegates_to_live_writer():
    client, _ = client_with_row(None)
    client.save_platform_dialer_credentials = AsyncMock()
    await client.save_platform_dialer_twilio_credentials(account_sid="account")
    client.save_platform_dialer_credentials.assert_awaited_once_with(account_sid="account")


@pytest.mark.asyncio
async def test_unconfigured_status_does_not_require_decryption():
    client, session = client_with_row(None)
    status = await client.get_platform_dialer_connection_status()
    assert status["configured"] is False
    session.get.assert_awaited_once_with(PlatformTwilioDialerCredentialsModel, 1)


@pytest.mark.asyncio
async def test_signalwire_preserves_existing_credentials_shape():
    row = SimpleNamespace(credentials={"project_id": "project"}, updated_at="now")
    client, session = client_with_row(row)
    assert await client.get_platform_signalwire_dialer_credentials() == {
        "project_id": "project", "updated_at": "now"}
    await client.save_platform_signalwire_dialer_credentials({"project_id": "next"})
    assert row.credentials == {"project_id": "next"}
    session.get.assert_awaited_with(PlatformSignalWireDialerCredentialsModel, 1)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_ai_voice_edits_cannot_change_dialer_credentials():
    row = SimpleNamespace(label="AI", account_sid="ai-account", auth_token_encrypted="encrypted")
    client, session = client_with_row(row)
    result = await client.update_platform_twilio_account(7, {
        "label": "Updated", "dialer_api_key_sid": "deprecated",
        "dialer_api_key_secret": "deprecated", "dialer_twiml_app_sid": "deprecated",
        "dialer_default_caller_id": "deprecated"})
    assert result is None and row.label == "Updated"
    assert vars(row) == dict(label="Updated", account_sid="ai-account", auth_token_encrypted="encrypted")
    session.commit.assert_awaited_once()
