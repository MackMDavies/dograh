"""Exercise the actual dispatchers: a capped number must never place a call."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.services.campaign import campaign_call_dispatcher as dispatch
from api.services.campaign.errors import PhoneNumberDailyLimitError, PhoneNumberPoolExhaustedError
from api.services.campaign.rate_limiter import FromNumberLease
from api.services.telephony import number_policy

NY = '+12125550101'


@pytest.mark.parametrize('pool_exhausted,redis_failed', [(True, False), (False, False), (False, True)])
async def test_campaign_limit_defers_without_creating_a_run_and_releases_capacity(pool_exhausted, redis_failed):
    dispatcher = dispatch.CampaignCallDispatcher()
    campaign = SimpleNamespace(workflow_id=55, organization_id=7, telephony_configuration_id=170)
    queued = SimpleNamespace(id=101, context_variables={'phone_number': '+12125550102'})
    provider = SimpleNamespace(PROVIDER_NAME='twilio', initiate_call=AsyncMock())
    reservation = AsyncMock(side_effect=RuntimeError('offline')) if redis_failed else AsyncMock(return_value=False)
    acquire = AsyncMock(side_effect=PhoneNumberDailyLimitError(7)) if pool_exhausted else AsyncMock(return_value=FromNumberLease(NY, 'lease-1'))
    with (patch.object(dispatch, 'db_client') as db, patch.object(dispatch, 'rate_limiter') as rl,
          patch.object(dispatch, 'is_number_suppressed', AsyncMock(return_value=False)),
          patch.object(dispatcher, 'get_provider_for_campaign', AsyncMock(return_value=provider)),
          patch.object(dispatcher, 'acquire_from_number', acquire),
          patch.object(number_policy, 'load_number_policies', AsyncMock(return_value={NY: number_policy.NumberPolicy(7, 20, NY, 50, True)})),
          patch.object(number_policy, 'reserve_daily_attempt', reservation)):
        db.get_workflow_by_id = AsyncMock(return_value=SimpleNamespace(id=55))
        db.create_workflow_run = AsyncMock()
        rl.release_concurrent_slot = AsyncMock()
        rl.release_from_number = AsyncMock()
        with pytest.raises(PhoneNumberPoolExhaustedError):
            await dispatcher.dispatch_call(queued, campaign, 'slot-1')
        rl.release_concurrent_slot.assert_awaited_once_with(7, 'slot-1')
        if pool_exhausted:
            rl.release_from_number.assert_not_awaited()
        else:
            rl.release_from_number.assert_awaited_once_with(7, NY, telephony_configuration_id=170, lease_id='lease-1')
        db.create_workflow_run.assert_not_awaited()
        provider.initiate_call.assert_not_awaited()


@pytest.mark.parametrize('limited', [True, False])
def test_manual_call_respects_number_limit_and_preserves_execution_owner(limited):
    from api.routes import telephony
    from api.services.auth.depends import get_user
    app = FastAPI(); app.include_router(telephony.router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(id=7, selected_organization_id=11, is_superuser=False)
    provider = SimpleNamespace(PROVIDER_NAME='twilio', WEBHOOK_ENDPOINT='twiml', from_numbers=[NY], validate_config=lambda: True,
        initiate_call=AsyncMock(return_value=SimpleNamespace(provider_metadata={}, caller_number=NY)))
    selection = AsyncMock(side_effect=HTTPException(429, 'Daily limit')) if limited else AsyncMock(return_value=NY)
    with (patch.object(telephony, 'db_client') as db,
          patch.object(telephony, 'get_default_telephony_provider', AsyncMock(return_value=provider)),
          patch.object(telephony, 'check_dograh_quota_by_user_id', AsyncMock(return_value=SimpleNamespace(has_quota=True))),
          patch('api.services.workflow_active_check.check_workflow_active', AsyncMock(return_value=(True, None))),
          patch('api.services.wallet_check.check_wallet_before_call', AsyncMock(return_value=(True, ''))),
          patch('api.services.telephony.call_concurrency.acquire_call_slot', AsyncMock(return_value=(True, 'slot-1'))),
          patch('api.services.telephony.call_concurrency.bind_slot', AsyncMock()),
          patch('api.services.telephony.call_concurrency.release_slot_for_failed_start', AsyncMock()) as release,
          patch.object(telephony, 'get_backend_endpoints', AsyncMock(return_value=('https://example.invalid','wss://example.invalid'))),
          patch.object(number_policy, 'reserve_outbound_number', selection)):
        db.get_user_configurations = AsyncMock(return_value=SimpleNamespace(test_phone_number=None))
        db.get_default_telephony_configuration = AsyncMock(return_value=SimpleNamespace(id=55))
        db.get_workflow = AsyncMock(return_value=SimpleNamespace(id=33, user_id=99, organization_id=11))
        db.create_workflow_run = AsyncMock(return_value=SimpleNamespace(id=501, initial_context={}))
        db.update_workflow_run = AsyncMock()
        response = TestClient(app).post('/telephony/initiate-call', json={'workflow_id': 33, 'phone_number': '+12125550102'})
        if limited:
            assert response.status_code == 429
            release.assert_awaited_once_with(11, 'slot-1')
            db.create_workflow_run.assert_not_awaited()
            provider.initiate_call.assert_not_awaited()
        else:
            assert response.status_code == 200
            release.assert_not_awaited()
            assert provider.initiate_call.await_args.kwargs['from_number'] == NY
            assert provider.initiate_call.await_args.kwargs['user_id'] == 99


def test_capability_endpoint_advertises_only_implemented_preferences():
    from api.routes.telephony import router
    from api.services.auth.depends import get_user
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(id=7)
    result = TestClient(app).get('/telephony/number-preferences/capabilities').json()
    assert result == {
        'daily_call_cap': True,
        'local_presence': True,
        'inbound_number_routing_providers': ['twilio'],
        'inbound_sms_providers': ['twilio'],
    }
