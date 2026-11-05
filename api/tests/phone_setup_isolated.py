"""Dependency-free contract probes; executes the actual methods/handlers with provider/DB mocks.
Full FastAPI route tests remain in test_telephony_quick_connect.py.
"""
import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase, main
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
class ProviderError(Exception):
    pass
class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail

def load_function(file, name, context):
    tree = ast.parse((ROOT/file).read_text())
    candidates = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
    node = candidates[0]
    node.decorator_list = []
    node.returns = None
    node.args.defaults = []
    for arg in node.args.args + node.args.kwonlyargs:
        arg.annotation = None
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(file), 'exec'), context)
    return context[name]

class ProvisionerTests(TestCase):
    def test_types_follow_twilio_country_capabilities(self):
        client = MagicMock()
        client.available_phone_numbers.return_value.fetch.return_value.subresource_uris = {'local':'/local','toll_free':'/tollfree'}
        method = load_function(Path('services/telephony/managed_provisioner.py'), 'available_number_types', {'TwilioRestException': ProviderError})
        self.assertEqual(method(SimpleNamespace(_client=client), 'US'), ['local','toll_free'])
        client.available_phone_numbers.assert_called_once_with('US')

    def test_inbound_test_excludes_old_and_outbound_calls(self):
        now = datetime.now(timezone.utc)
        client = MagicMock()
        method = load_function(Path('services/telephony/managed_provisioner.py'), 'has_inbound_call_since', {})
        client.calls.list.return_value = [SimpleNamespace(date_created=now-timedelta(minutes=1),direction='inbound',to='+12125550101'), SimpleNamespace(date_created=now,direction='outbound-api',to='+12125550101')]
        self.assertFalse(method(SimpleNamespace(_client=client), '+12125550101', now))
        client.calls.list.return_value.append(SimpleNamespace(date_created=now,direction='inbound',to='+12125550101'))
        self.assertTrue(method(SimpleNamespace(_client=client), '+12125550101', now))

class RouteTests(IsolatedAsyncioTestCase):
    def context(self):
        return {'datetime':datetime,'timedelta':timedelta,'timezone':timezone,'HTTPException':HTTPException,'TwilioRestException':ProviderError,'asyncio':asyncio,'db_client':SimpleNamespace(get_phone_number=AsyncMock(return_value=None)),'_provisioner_for_number':AsyncMock()}
    async def test_forwarding_rejects_foreign_number(self):
        context = self.context()
        method = load_function(Path('routes/telephony_quick_connect.py'), 'forwarding_check', context)
        with self.assertRaises(HTTPException) as error:
            await method(42,datetime.now(timezone.utc),SimpleNamespace(selected_organization_id=11))
        self.assertEqual(error.exception.status_code,404)
        context['db_client'].get_phone_number.assert_awaited_once_with(42,organization_id=11)
        context['_provisioner_for_number'].assert_not_called()
    async def test_forwarding_rejects_stale_test_without_db_read(self):
        context = self.context()
        method = load_function(Path('routes/telephony_quick_connect.py'), 'forwarding_check', context)
        with self.assertRaises(HTTPException) as error:
            await method(42,datetime.now(timezone.utc)-timedelta(minutes=10),SimpleNamespace(selected_organization_id=11))
        self.assertEqual(error.exception.status_code,422)
        context['db_client'].get_phone_number.assert_not_called()
    async def test_forwarding_returns_real_observation_only(self):
        context=self.context()
        context['db_client'].get_phone_number.return_value=SimpleNamespace(address='+12125550101',extra_metadata={'is_managed':True})
        provider=MagicMock();provider.has_inbound_call_since.return_value=True
        context['_provisioner_for_number'].return_value=provider
        method=load_function(Path('routes/telephony_quick_connect.py'),'forwarding_check',context)
        now=datetime.now(timezone.utc)
        self.assertEqual(await method(42,now,SimpleNamespace(selected_organization_id=11)),{'observed':True})
        provider.has_inbound_call_since.assert_called_once_with('+12125550101',now)
    async def test_state_filter_reaches_the_provider_and_failures_are_not_empty_success(self):
        context=self.context()
        provider=MagicMock();provider.search_available_numbers_detailed.return_value=[{'phone_number':'+14155550101','region':'CA'}]
        context.update(get_managed_provisioner=AsyncMock(return_value=provider),_resolve_platform_account_id=lambda user, platform:None,AvailableNumbersResponse=lambda **data:data)
        method=load_function(Path('routes/telephony_quick_connect.py'),'available_numbers',context)
        args=('US',None,None,'local',None,None,'ca',True,SimpleNamespace(selected_organization_id=11))
        self.assertEqual((await method(*args))['items'][0]['region'],'CA')
        provider.search_available_numbers_detailed.assert_called_once_with('US','local',None,None,None,in_region='CA')
        provider.search_available_numbers_detailed.side_effect=ProviderError()
        with self.assertRaises(HTTPException) as error:
            await method(*args)
        self.assertEqual(error.exception.status_code,502)

    async def test_country_list_obeys_allowance_and_provider(self):
        context=self.context()
        provider=MagicMock();provider.available_number_types.return_value=['local','toll_free']
        context.update(get_managed_provisioner=AsyncMock(return_value=provider),number_scope=AsyncMock(return_value={'account_id':'owned'}),request_number_policy=AsyncMock(side_effect=lambda scope, body:{'allowed':body['country']=='US'}),ManagedNumberPolicyError=ProviderError)
        method=load_function(Path('routes/telephony_quick_connect.py'),'available_number_countries',context)
        self.assertEqual(await method(SimpleNamespace(selected_organization_id=11)),{'countries':[{'code':'US','name':'United States','types':['local','toll_free']}]})
        provider.available_number_types.assert_called_once_with('US')

if __name__ == '__main__':
    main()
