"""Saved outbound-number limits: scoped loading, local choice and hard daily cap."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from api.services.telephony import number_policy as policy

ACCOUNT = '11111111-1111-4111-8111-111111111111'
NY = '+12125550101'
LA = '+13105550101'


@pytest.fixture(autouse=True)
def clean_cache():
    policy._cache.clear()
    yield
    policy._cache.clear()


def test_account_comes_only_from_the_account_scoped_org():
    assert policy.account_from_org('supabase_org_acct_' + ACCOUNT) == ACCOUNT
    assert policy.account_from_org('supabase_org_' + ACCOUNT) is None
    assert policy.account_from_org('supabase_org_acct_not-a-uuid') is None
    assert policy.account_from_org(None) is None


def test_local_calendar_day_survives_dst_and_does_not_mix_tenants():
    p = policy.NumberPolicy(10, 20, NY, 50, timezone='America/New_York')
    assert p.day_key(datetime(2026, 11, 1, 3, 30, tzinfo=UTC)).endswith('2026-10-31')
    assert p.day_key(datetime(2026, 11, 1, 6, 30, tzinfo=UTC)).endswith('2026-11-01')
    assert policy.NumberPolicy(11, 20, NY, 50).day_key() != policy.NumberPolicy(10, 20, NY, 50).day_key()
    assert policy.NumberPolicy(10, 20, NY, 50, timezone='unknown').day_key(datetime(2026, 1, 1, tzinfo=UTC)).endswith('2026-01-01')


def test_geographic_presence_matches_real_area_codes_and_ignores_mobile_prefixes():
    assert policy.geographic_area(NY) == policy.geographic_area('+12125550102')
    assert policy.geographic_area(NY) != policy.geographic_area(LA)
    assert policy.geographic_area('+442079460100') == (44, '20')
    assert policy.geographic_area('+447700900123') is None
    assert policy.geographic_area('sip:desk@example.invalid') is None


@pytest.fixture
def loader(monkeypatch):
    monkeypatch.setattr(policy, 'SUPABASE_URL', 'https://example.invalid')
    monkeypatch.setattr(policy, 'SUPABASE_SERVICE_ROLE_KEY', 'fake-test-key')
    monkeypatch.setattr(policy.db_client, 'get_telephony_configuration', AsyncMock(return_value=SimpleNamespace(id=3, organization_id=10)))
    monkeypatch.setattr(policy.db_client, 'get_organization_by_id', AsyncMock(return_value=SimpleNamespace(provider_id='supabase_org_acct_' + ACCOUNT)))
    rows = [SimpleNamespace(id=20, organization_id=10, is_active=True, address_normalized=NY),
            SimpleNamespace(id=21, organization_id=99, is_active=True, address_normalized=LA)]
    monkeypatch.setattr(policy.db_client, 'list_phone_numbers_for_config', AsyncMock(return_value=rows))
    calls = []
    settings = [{'dograh_phone_number_id': 20, 'e164': NY, 'daily_call_cap': 100, 'local_presence': True, 'timezone': 'America/New_York'},
                {'dograh_phone_number_id': 21, 'e164': LA, 'daily_call_cap': 100}]
    original = httpx.AsyncClient
    def reply(request):
        calls.append(request)
        return httpx.Response(200, json=settings)
    monkeypatch.setattr(policy.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(reply), **kw))
    return calls, settings


async def test_load_is_scoped_to_actual_config_owner_and_full_number(loader):
    calls, _ = loader
    result = await policy.load_number_policies(10, 3)
    assert list(result) == [NY]
    assert result[NY].daily_limit == 100
    assert calls[0].url.params['client_account_id'] == 'eq.' + ACCOUNT
    assert calls[0].url.params['dograh_phone_number_id'] == 'in.(20)'
    assert await policy.load_number_policies(10, 3) == result
    assert len(calls) == 1


async def test_wrong_phone_and_invalid_limits_are_never_applied(loader):
    _, rows = loader
    for change in [{'e164': '+19995550100'}, {'daily_call_cap': 0}, {'daily_call_cap': True}]:
        original = dict(rows[0]); rows[0].update(change)
        assert await policy.load_number_policies(10, 3) == {}
        rows[0] = original; policy._cache.clear()


async def test_failed_refresh_retains_last_known_limit(loader, monkeypatch):
    _, rows = loader
    first = await policy.load_number_policies(10, 3)
    policy._cache[(10, 3)] = (0, first)
    monkeypatch.setattr(policy.db_client, 'get_telephony_configuration', AsyncMock(side_effect=RuntimeError('offline')))
    assert await policy.load_number_policies(10, 3) == first


async def test_legacy_org_does_not_read_another_accounts_preferences(loader, monkeypatch):
    calls, _ = loader
    monkeypatch.setattr(policy.db_client, 'get_organization_by_id', AsyncMock(return_value=SimpleNamespace(provider_id='supabase_org_' + ACCOUNT)))
    assert await policy.load_number_policies(10, 3) == {}
    assert calls == []


async def test_unconfigured_calls_keep_provider_selection(monkeypatch):
    monkeypatch.setattr(policy, 'load_number_policies', AsyncMock(return_value={}))
    assert await policy.reserve_outbound_number(10, 3, NY, [NY, LA]) is None
    assert await policy.reserve_outbound_number(10, 3, NY, [NY, LA], LA) == LA


async def test_presence_uses_matching_owned_number_then_falls_back_when_capped(monkeypatch):
    policies = {NY: policy.NumberPolicy(10, 20, NY, 50, True), LA: policy.NumberPolicy(10, 21, LA, 50, True)}
    monkeypatch.setattr(policy, 'load_number_policies', AsyncMock(return_value=policies))
    monkeypatch.setattr(policy.random, 'shuffle', lambda _: None)
    reserve = AsyncMock(side_effect=[False, True]); monkeypatch.setattr(policy, 'reserve_daily_attempt', reserve)
    assert await policy.reserve_outbound_number(10, 3, '+12125550102', [LA, NY]) == LA
    assert reserve.await_args_list[0].args[0].number == NY


async def test_explicit_caller_id_is_never_silently_replaced_at_its_limit(monkeypatch):
    monkeypatch.setattr(policy, 'load_number_policies', AsyncMock(return_value={NY: policy.NumberPolicy(10, 20, NY, 50)}))
    monkeypatch.setattr(policy, 'reserve_daily_attempt', AsyncMock(return_value=False))
    with pytest.raises(HTTPException) as exc:
        await policy.reserve_outbound_number(10, 3, LA, [NY, LA], NY)
    assert exc.value.status_code == 429


async def test_known_limit_does_not_fail_open_when_redis_is_down(monkeypatch):
    monkeypatch.setattr(policy, 'load_number_policies', AsyncMock(return_value={NY: policy.NumberPolicy(10, 20, NY, 50)}))
    monkeypatch.setattr(policy, 'reserve_daily_attempt', AsyncMock(side_effect=RuntimeError('offline')))
    with pytest.raises(HTTPException) as exc:
        await policy.reserve_outbound_number(10, 3, LA, [NY], NY)
    assert exc.value.status_code == 503
