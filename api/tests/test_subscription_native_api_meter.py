import asyncio
from types import SimpleNamespace
import unittest
from api.services.subscription_api_meter import meter_native_api_request, NativeApiMeterError

ACCOUNT='10000000-0000-4000-8000-000000000001'
class NativeMeterTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, *, plan=None, provider='supabase_org_acct_'+ACCOUNT, reply=None):
        calls=[]
        async def org(identity):
            self.assertEqual(identity,42)
            return SimpleNamespace(provider_id=provider)
        async def config(org_id,name):
            self.assertEqual((org_id,name),(42,'SYSEVO_PLAN_KEY'))
            return SimpleNamespace(value={'value':plan}) if plan is not None else None
        async def post(payload):
            calls.append(payload)
            if isinstance(reply,Exception):raise reply
            return {'applicable':True,'allowed':True} if reply is None else reply
        return SimpleNamespace(id=7,organization_id=42), SimpleNamespace(get_organization_by_id=org,get_configuration=config),post,calls

    async def test_key_owned_organization_and_server_request_ids_are_used(self):
        key,db,post,calls=self.fixture()
        key.selected_organization_id=999
        self.assertTrue(await meter_native_api_request(key,db,post=post))
        self.assertTrue(await meter_native_api_request(key,db,post=post))
        self.assertEqual(calls[0]['account_id'],ACCOUNT)
        self.assertEqual(calls[0]['native_api_key_id'],7)
        self.assertNotEqual(calls[0]['request_id'],calls[1]['request_id'])
        self.assertEqual(set(calls[0]),{'account_id','native_api_key_id','request_id','is_mcp'})

    async def test_mcp_is_explicit_and_browser_style_claims_cannot_select_it(self):
        key,db,post,calls=self.fixture()
        self.assertTrue(await meter_native_api_request(key,db,post=post,is_mcp=True))
        self.assertIs(calls[0]['is_mcp'],True)
        for value in ('true',1,None):
            with self.assertRaises(NativeApiMeterError):await meter_native_api_request(key,db,post=post,is_mcp=value)
        self.assertEqual(len(calls),1)

    async def test_database_failures_are_sanitized_and_fail_closed(self):
        key,db,post,_=self.fixture()
        async def fail(*_):raise RuntimeError('private-database-url')
        db.get_organization_by_id=fail
        with self.assertRaises(NativeApiMeterError) as error:await meter_native_api_request(key,db,post=post)
        self.assertEqual(error.exception.status_code,503)
        self.assertNotIn('private-database-url',str(error.exception))

    async def test_native_oss_has_no_new_platform_dependency(self):
        key,db,post,calls=self.fixture(provider='local-org')
        self.assertFalse(await meter_native_api_request(key,db,post=post))
        self.assertEqual(calls,[])

    async def test_unknown_or_self_serve_outage_denies_without_private_error(self):
        for plan in (None,'subscription_voice'):
            key,db,post,calls=self.fixture(plan=plan,reply=RuntimeError('private-secret'))
            with self.assertRaises(NativeApiMeterError) as error:
                await meter_native_api_request(key,db,post=post)
            self.assertEqual(error.exception.status_code,503)
            self.assertNotIn('private-secret',str(error.exception))

    async def test_explicit_enterprise_outage_retains_legacy_accounting(self):
        key,db,post,_=self.fixture(plan='command',reply=RuntimeError('unavailable'))
        self.assertFalse(await meter_native_api_request(key,db,post=post))

    async def test_authoritative_legacy_reply_preserves_the_existing_path(self):
        key,db,post,_=self.fixture(reply={'applicable':False,'allowed':True})
        self.assertFalse(await meter_native_api_request(key,db,post=post))

    async def test_self_serve_cannot_become_legacy_after_a_route_mismatch(self):
        key,db,post,_=self.fixture(plan='subscription_voice',reply={'applicable':False,'allowed':True})
        with self.assertRaises(NativeApiMeterError):await meter_native_api_request(key,db,post=post)

    async def test_exhaustion_denies429_and_other_entitlement_denials403(self):
        for reason,status in [('monthly_request_allowance_reached',429),('paid_terms_missing',403)]:
            key,db,post,_=self.fixture(reply={'applicable':True,'allowed':False,'reason':reason})
            with self.assertRaises(NativeApiMeterError) as error:await meter_native_api_request(key,db,post=post)
            self.assertEqual(error.exception.status_code,status)

    async def test_invalid_replies_cannot_allow_unmetered_usage(self):
        for reply in ({},{'applicable':False,'allowed':False},{'applicable':True,'allowed':'true'},[]):
            key,db,post,_=self.fixture(reply=reply)
            with self.assertRaises(NativeApiMeterError):await meter_native_api_request(key,db,post=post)

    async def test_key_identity_is_validated_before_platform_request(self):
        key,db,post,calls=self.fixture()
        for value in (True,0,-1,'7'):
            key.id=value
            with self.assertRaises(NativeApiMeterError):await meter_native_api_request(key,db,post=post)
        self.assertEqual(calls,[])

    async def test_malformed_platform_binding_never_reaches_the_service(self):
        key,db,post,calls=self.fixture(provider='supabase_org_acct_foreign')
        with self.assertRaises(NativeApiMeterError):await meter_native_api_request(key,db,post=post)
        self.assertEqual(calls,[])

if __name__=='__main__':unittest.main()
