"""Exercise the real pre-carrier slot helper with synthetic transport/session seams."""
import sys,types,unittest
from types import SimpleNamespace
from unittest.mock import patch
from api.services.subscription_managed_numbers import managed_number_slot,ManagedNumberPolicyError,number_scope,release_subscription_number,cleanup_released_number
ACCOUNT='00000000-0000-4000-8000-000000000001'
class DB:
    def __init__(self,used=0,plan='subscription_voice',recorded=0):self.used=used;self.plan=plan;self.recorded=recorded;self.events=[]
    async def get_organization_by_id(self,org):return SimpleNamespace(provider_id='supabase_org_acct_'+ACCOUNT)
    async def get_configuration(self,org,key):return SimpleNamespace(value={'value':self.plan})
    async def delete_phone_number(self,phone,organization_id):self.events.append(('delete',phone,organization_id))
    def async_session(self):return Session(self)
class Session:
    def __init__(self,db):self.db=db
    async def __aenter__(self):return self
    async def __aexit__(self,*_):self.db.events.append('session-exit')
    def begin(self):return self
    async def execute(self,sql,args):
        self.db.events.append('lock' if 'pg_advisory' in sql else 'count')
        return SimpleNamespace(scalar=lambda:self.db.used)
class Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        module=types.ModuleType('sqlalchemy');module.text=lambda sql:sql
        self.stub=patch.dict(sys.modules,{'sqlalchemy':module});self.stub.start();self.addCleanup(self.stub.stop)
    def bridge(self,db,limit=1):
        calls=[]
        async def post(payload):
            calls.append(payload)
            if payload['operation']=='context':return {'applicable':True,'allowed':True,'limit':limit,'recorded_used':db.recorded}
            return {'applicable':True,'allowed':True,'recorded':True,'replayed':False,'released':True}
        return post,calls
    async def test_all_five_caps_reserve_before_any_provider_work(self):
        for cap in (1,1,2,3,5):
            db=DB(used=cap-1);post,calls=self.bridge(db,cap)
            async with managed_number_slot(42,db,'US',post=post) as slot:
                self.assertEqual(db.events,['lock','count'])
                self.assertEqual(slot.used,cap)
                await slot.register(SimpleNamespace(id=7,organization_id=42,country_code='US',address='+15555550123'))
            self.assertEqual(calls[-1],{'account_id':ACCOUNT,'operation':'register','country':'US','organization_id':42,'phone_number_id':7,'e164':'+15555550123','used':cap})
            self.assertIn('session-exit',db.events)
    async def test_native_or_platform_full_capacity_prevents_provider_work(self):
        for db in (DB(used=1),DB(recorded=1)):
            post,_=self.bridge(db)
            with self.assertRaises(ManagedNumberPolicyError) as error:
                async with managed_number_slot(42,db,'US',post=post):self.fail('Provider path reached at capacity')
            self.assertEqual(error.exception.status_code,403)
            self.assertIn('session-exit',db.events)
    async def test_unpaid_or_unlaunched_country_denies_before_lock_and_provider(self):
        for reason in ('paid_subscription_required','subscription_country_not_supported'):
            db=DB()
            async def denied(_):return {'applicable':True,'allowed':False,'reason':reason}
            with self.assertRaises(ManagedNumberPolicyError):
                async with managed_number_slot(42,db,'US',post=denied):self.fail('Provider path reached')
            self.assertEqual(db.events,[])
    async def test_policy_is_revalidated_after_waiting_for_the_lock(self):
        db=DB();calls=0
        async def post(_):
            nonlocal calls
            calls+=1
            return {'applicable':True,'allowed':calls==1,'limit':1,'recorded_used':0}
        with self.assertRaises(ManagedNumberPolicyError):
            async with managed_number_slot(42,db,'US',post=post):self.fail('Stale policy reached provider')
        self.assertEqual(db.events[0],'lock');self.assertNotIn('count',db.events)
    async def test_unknown_transport_failure_is_closed_but_explicit_enterprise_is_preserved(self):
        async def unavailable(_):raise RuntimeError('private transport detail')
        db=DB()
        with self.assertRaises(ManagedNumberPolicyError):
            async with managed_number_slot(42,db,'US',post=unavailable):self.fail('Unknown subscription was admitted')
        db=DB(plan='command')
        async with managed_number_slot(42,db,'GB',post=unavailable) as slot:self.assertIsNone(slot.scope)
        self.assertEqual(db.events,[])
    async def test_self_serve_marker_cannot_be_downgraded_by_a_legacy_reply(self):
        async def legacy(_):return {'applicable':False,'allowed':True}
        with self.assertRaises(ManagedNumberPolicyError):
            async with managed_number_slot(42,DB(),'US',post=legacy):self.fail('Legacy downgrade reached provider')
    async def test_invalid_capacity_replies_fail_closed(self):
        for limit,recorded in [(True,0),(1.5,0),(-1,0),(1,-1),(1,True)]:
            async def post(_):return {'applicable':True,'allowed':True,'limit':limit,'recorded_used':recorded}
            with self.assertRaises(ManagedNumberPolicyError):
                async with managed_number_slot(42,DB(),'US',post=post):self.fail('Malformed policy reached provider')
    async def test_registration_cannot_switch_native_organization(self):
        db=DB();post,calls=self.bridge(db)
        async with managed_number_slot(42,db,'US',post=post) as slot:
            with self.assertRaises(ManagedNumberPolicyError):
                await slot.register(SimpleNamespace(id=7,organization_id=99,country_code='US',address='+15555550123'))
        self.assertEqual([c['operation'] for c in calls],['context','context'])
    async def test_release_uses_the_same_trusted_account_identity(self):
        db=DB();post,calls=self.bridge(db)
        async with managed_number_slot(42,db,'US',post=post) as slot:await slot.release(7)
        self.assertEqual(calls[-1],{'account_id':ACCOUNT,'operation':'release','phone_number_id':7})
    async def test_invalid_organization_identity_is_never_accepted(self):
        for org in (True,0,-1,'42'):
            with self.assertRaises(ManagedNumberPolicyError):await number_scope(org,DB())

    async def test_release_confirms_carrier_before_freeing_native_and_billing_slots(self):
        db=DB();post,calls=self.bridge(db)
        row=SimpleNamespace(id=7,organization_id=42,country_code='US',extra_metadata={'managed_twilio_sid':'PNsynthetic'})
        provider=SimpleNamespace(release_number=lambda sid:db.events.append('carrier-release') or True)
        self.assertTrue(await release_subscription_number(row,provider,db,post=post))
        self.assertEqual(db.events,['carrier-release',('delete',7,42)])
        self.assertEqual(calls[-1]['operation'],'release')
    async def test_failed_carrier_release_retains_the_native_capacity_slot(self):
        db=DB();post,calls=self.bridge(db)
        row=SimpleNamespace(id=7,organization_id=42,country_code='US',extra_metadata={'managed_twilio_sid':'PNsynthetic'})
        provider=SimpleNamespace(release_number=lambda _:False,_client=SimpleNamespace(incoming_phone_numbers=lambda _:SimpleNamespace(fetch=lambda:object())))
        with self.assertRaises(ManagedNumberPolicyError):await release_subscription_number(row,provider,db,post=post)
        self.assertEqual(db.events,[]);self.assertEqual([c['operation'] for c in calls],['context'])
    async def test_already_absent_carrier_resource_can_finish_release_after_a_lost_response(self):
        db=DB();post,_=self.bridge(db)
        class Missing(Exception):status=404
        def absent():raise Missing()
        row=SimpleNamespace(id=7,organization_id=42,country_code='US',extra_metadata={'managed_twilio_sid':'PNsynthetic'})
        provider=SimpleNamespace(release_number=lambda _:False,_client=SimpleNamespace(incoming_phone_numbers=lambda _:SimpleNamespace(fetch=absent)))
        self.assertTrue(await release_subscription_number(row,provider,db,post=post))
        self.assertEqual(db.events,[('delete',7,42)])
    async def test_expired_paid_access_does_not_block_own_number_cleanup(self):
        db=DB();calls=[]
        async def post(body):
            calls.append(body)
            return {'applicable':True,'allowed':False} if body['operation']=='context' else {'applicable':True,'allowed':True,'released':True}
        self.assertTrue(await cleanup_released_number(42,7,db,post=post))
        self.assertEqual(calls[-1],{'account_id':ACCOUNT,'operation':'release','phone_number_id':7})
    async def test_actual_quick_connect_route_blocks_before_any_provider_call(self):
        import ast
        from pathlib import Path
        db=DB(used=1);post,_=self.bridge(db);provider_calls=[]
        def context(org,client,country):return managed_number_slot(org,client,country,post=post)
        async def provider(*_):provider_calls.append('provider');return object()
        class HTTPError(Exception):
            def __init__(self,status_code,detail):self.status_code=status_code;self.detail=detail
        path=Path(__file__).parents[1]/'routes/telephony_quick_connect.py'
        function=next(node for node in ast.parse(path.read_text()).body if isinstance(node,ast.AsyncFunctionDef) and node.name=='quick_connect')
        function.decorator_list=[]
        ns={'QuickConnectRequest':object,'QuickConnectResponse':object,'UserModel':object,'Depends':lambda *_:None,'get_user':lambda:None,
          'db_client':db,'managed_number_slot':context,'ManagedNumberPolicyError':ManagedNumberPolicyError,'HTTPException':HTTPError,'_provision_quick_connect':provider}
        exec(compile(ast.Module(body=[function],type_ignores=[]),str(path),'exec'),ns)
        with self.assertRaises(HTTPError) as error:
            await ns['quick_connect'](SimpleNamespace(mode='new',existing_number=None,country='US'),SimpleNamespace(selected_organization_id=42))
        self.assertEqual(error.exception.status_code,403);self.assertEqual(provider_calls,[])

if __name__=='__main__':unittest.main()
