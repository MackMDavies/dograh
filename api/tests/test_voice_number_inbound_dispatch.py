from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.routes.telephony import handle_inbound_run
from api.services.telephony.number_policy import NumberPolicy
from api.services.telephony.providers.twilio.provider import TwilioProvider
from api.services.telephony.base import NormalizedInboundData


ORG_ID = 71
WORKFLOW_ID = 81
PHONE_ID = 91
CONFIG_ID = 101
CALL_SID = "CA123"
INBOUND = NormalizedInboundData(
    provider="twilio", call_id=CALL_SID,
    from_number="+15551230001", to_number="+15551230002",
    direction="inbound", call_status="ringing", account_id="AC123",
)


def _policy(**changes):
    values = dict(
        organization_id=ORG_ID,
        number_id=PHONE_ID,
        number=INBOUND.to_number,
        daily_limit=300,
        timezone="UTC",
        business_hours_enabled=True,
        business_days=(),
        after_hours="vm",
        on_fail="vm",
        forward_to=None,
    )
    values.update(changes)
    return NumberPolicy(**values)


@pytest.mark.asyncio
async def test_signed_after_hours_forward_returns_provider_native_forward_without_ai_run():
    provider = TwilioProvider({"account_sid": "AC123", "auth_token": "test-token"})
    provider.verify_inbound_signature = AsyncMock(return_value=True)
    config = SimpleNamespace(id=CONFIG_ID, organization_id=ORG_ID)
    phone = SimpleNamespace(id=PHONE_ID, address_normalized=INBOUND.to_number, inbound_workflow_id=WORKFLOW_ID)
    workflow = SimpleNamespace(id=WORKFLOW_ID, user_id=111)
    request = SimpleNamespace(url="https://example.test/api/v1/telephony/inbound/run", headers={})

    with (
        patch("api.routes.telephony.parse_webhook_request", new_callable=AsyncMock, return_value=({"CallSid": CALL_SID}, "raw")),
        patch("api.routes.telephony._detect_provider", new_callable=AsyncMock, return_value=TwilioProvider),
        patch("api.routes.telephony.normalize_webhook_data", return_value=INBOUND),
        patch("api.services.telephony.registry.get_optional", return_value=SimpleNamespace(account_id_credential_field="account_sid")),
        patch("api.routes.telephony.db_client.find_inbound_route_by_account", new_callable=AsyncMock, return_value=(config, phone)),
        patch("api.routes.telephony.db_client.get_workflow", new_callable=AsyncMock, return_value=workflow),
        patch("api.routes.telephony.get_telephony_provider_by_id", new_callable=AsyncMock, return_value=provider),
        patch("api.services.telephony.number_policy.load_inbound_number_policy", new_callable=AsyncMock, return_value=_policy(after_hours="fwd", forward_to="+15551230003")),
        patch("api.routes.telephony.get_backend_endpoints", new_callable=AsyncMock, return_value=("https://api.example.test", "wss://api.example.test")),
        patch("api.routes.telephony.check_dograh_quota_by_user_id", new_callable=AsyncMock) as quota,
        patch("api.routes.telephony.db_client.create_workflow_run", new_callable=AsyncMock) as create_run,
        patch("api.services.workflow_active_check.check_workflow_active", new_callable=AsyncMock) as active_check,
    ):
        response = await handle_inbound_run(request)

    assert b"<Dial" in response.body
    assert b"+15551230003" in response.body
    provider.verify_inbound_signature.assert_awaited_once()
    quota.assert_not_awaited()
    active_check.assert_not_awaited()
    create_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_signed_after_hours_voicemail_creates_idempotent_recording_owner_run():
    provider = TwilioProvider({"account_sid": "AC123", "auth_token": "test-token"})
    provider.verify_inbound_signature = AsyncMock(return_value=True)
    config = SimpleNamespace(id=CONFIG_ID, organization_id=ORG_ID)
    phone = SimpleNamespace(id=PHONE_ID, address_normalized=INBOUND.to_number, inbound_workflow_id=WORKFLOW_ID)
    workflow = SimpleNamespace(id=WORKFLOW_ID, user_id=111)
    request = SimpleNamespace(url="https://example.test/api/v1/telephony/inbound/run", headers={})

    with (
        patch("api.routes.telephony.parse_webhook_request", new_callable=AsyncMock, return_value=({"CallSid": CALL_SID}, "raw")),
        patch("api.routes.telephony._detect_provider", new_callable=AsyncMock, return_value=TwilioProvider),
        patch("api.routes.telephony.normalize_webhook_data", return_value=INBOUND),
        patch("api.services.telephony.registry.get_optional", return_value=SimpleNamespace(account_id_credential_field="account_sid")),
        patch("api.routes.telephony.db_client.find_inbound_route_by_account", new_callable=AsyncMock, return_value=(config, phone)),
        patch("api.routes.telephony.db_client.get_workflow", new_callable=AsyncMock, return_value=workflow),
        patch("api.routes.telephony.get_telephony_provider_by_id", new_callable=AsyncMock, return_value=provider),
        patch("api.services.telephony.number_policy.load_inbound_number_policy", new_callable=AsyncMock, return_value=_policy()),
        patch("api.routes.telephony.get_backend_endpoints", new_callable=AsyncMock, return_value=("https://api.example.test", "wss://api.example.test")),
        patch("api.routes.telephony.db_client.create_workflow_run", new_callable=AsyncMock, return_value=SimpleNamespace(id=123)) as create_run,
        patch("api.services.workflow_active_check.check_workflow_active", new_callable=AsyncMock) as active_check,
        patch("api.routes.telephony.check_dograh_quota_by_user_id", new_callable=AsyncMock) as quota,
    ):
        response = await handle_inbound_run(request)

    assert b"<Record" in response.body
    assert b"/api/v1/telephony/twilio/inbound-voicemail/123" in response.body
    create_args = create_run.await_args.kwargs
    assert create_args["organization_id"] == ORG_ID
    assert create_args["idempotency_key"] == f"voice-number-voicemail:{CALL_SID}"
    assert create_args["annotations"]["voice_number_voicemail"]["enabled"] is True
    active_check.assert_not_awaited()
    quota.assert_not_awaited()
