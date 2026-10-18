"""An inbound routing cache must never reuse another account's agent."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.services.telephony.dialer import inbound_agent as routing


@pytest.fixture(autouse=True)
def clear_cache():
    routing._cache.clear()
    yield
    routing._cache.clear()


async def test_cache_is_partitioned_by_resolved_account():
    scopes=[{'client_account_id':'a','assignment':None}, {'client_account_id':'b','assignment':None}, {'client_account_id':'a','assignment':None}]
    a=SimpleNamespace(number='+14155550101');b=SimpleNamespace(number='+14155550102')
    with patch.object(routing,'inbound_scope',AsyncMock(side_effect=scopes)),patch.object(routing,'_resolve',AsyncMock(side_effect=[a,b])) as resolve:
        assert (await routing.current_inbound_agent(number='+14155550901')).number == a.number
        assert (await routing.current_inbound_agent(number='+14155550902')).number == b.number
        assert (await routing.current_inbound_agent(number='+14155550901')).number == a.number
    assert resolve.await_count == 2


async def test_unverified_inbound_number_does_not_fall_back_to_platform_agent(monkeypatch):
    monkeypatch.setenv('SYSEVO_SAM_INBOUND_NUMBER','+14155550100')
    with patch.object(routing,'inbound_scope',AsyncMock(return_value=None)),patch.object(routing,'_resolve',AsyncMock()) as resolve:
        assert (await routing.current_inbound_agent(number='+14155550999')).number == ''
    resolve.assert_not_awaited()


async def test_client_account_without_assignment_never_inherits_platform_environment(monkeypatch):
    monkeypatch.setenv('SYSEVO_SAM_INBOUND_NUMBER','+14155550100')
    client=await routing._resolve({'client_account_id':'client','platform':False,'assignment':None})
    platform=await routing._resolve({'client_account_id':'platform','platform':True,'assignment':None})
    assert client.number == '' and platform.number == '+14155550100'


async def test_status_lookup_uses_authenticated_user_and_bypasses_cache():
    with patch.object(routing,'inbound_scope',AsyncMock(return_value={'client_account_id':'a'})) as scope,patch.object(routing,'_resolve',AsyncMock(return_value=SimpleNamespace(number=''))):
        await routing.current_inbound_agent(fresh=True,user_id='rep')
    scope.assert_awaited_once_with(number=None,user_id='rep',call_id=None)
