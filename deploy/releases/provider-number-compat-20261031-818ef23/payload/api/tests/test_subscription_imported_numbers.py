"""Paid provider imports share the real counter query and existing org lock.

Carrier transport is synthetic; no calls, purchases or production DB access.
"""
import ast
import json
import sqlite3
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from api.services.subscription_managed_numbers import (
    imported_number_slot, managed_number_slot, ManagedNumberPolicyError,
)


class CounterDB:
    def __init__(self, numbers=(), plan='subscription_voice'):
        self.plan=plan;self.events=[]
        self.connection = sqlite3.connect(':memory:')
        self.connection.execute('CREATE TABLE telephony_phone_numbers(organization_id integer,extra_metadata text)')
        self.connection.executemany('INSERT INTO telephony_phone_numbers VALUES(?,?)', [(org,json.dumps(meta)) for org,meta in numbers])

    async def get_organization_by_id(self,org):return SimpleNamespace(provider_id='supabase_org_acct_00000000-0000-4000-8000-000000000001')
    async def get_configuration(self,org,key):return SimpleNamespace(value={'value':self.plan})

    def async_session(self):
        db=self
        class Session:
            async def __aenter__(self):return self
            async def __aexit__(self,*_):db.events.append('session-exit')
            def begin(self):return self
            async def execute(self,sql,args):
                if 'pg_advisory' in str(sql):
                    db.events.append('lock');return SimpleNamespace(scalar=lambda:None)
                db.events.append('count')
                # Execute the actual production COUNT_SQL, rather than returning
                # a preselected count that would hide the old managed-only filter.
                count=db.connection.execute(str(sql),args).fetchone()[0]
                return SimpleNamespace(scalar=lambda:count)
        return Session()


class Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        sqlalchemy=types.ModuleType('sqlalchemy');sqlalchemy.text=lambda text:text
        self.patch=patch.dict(sys.modules,{'sqlalchemy':sqlalchemy});self.patch.start();self.addCleanup(self.patch.stop)

    def db(self,*args,**kwargs):
        db=CounterDB(*args,**kwargs);self.addCleanup(db.connection.close);return db

    def policy(self,limit=1):
        async def post(body):return {'applicable':True,'allowed':True,'limit':limit,'recorded_used':0}
        return post

    async def test_imported_numbers_exhaust_all_five_tier_caps_before_provider_work(self):
        for limit in (1,1,2,3,5):
            db=self.db([(42,{'is_managed':False})]*limit)
            with self.assertRaises(ManagedNumberPolicyError) as error:
                async with imported_number_slot(42,db,'+12015550123',post=self.policy(limit)):
                    self.fail('Carrier/persistence reached at full capacity')
            self.assertEqual(error.exception.status_code,403)
            self.assertEqual(db.events[:2],['lock','count'])

    async def test_managed_purchase_counts_existing_provider_imports(self):
        db=self.db([(42,{'is_managed':False})])
        with self.assertRaises(ManagedNumberPolicyError):
            async with managed_number_slot(42,db,'US',post=self.policy()):self.fail('Would purchase beyond the shared cap')

    async def test_mixed_metadata_and_other_orgs_use_one_authoritative_account_counter(self):
        db=self.db([(42,{'is_managed':True}),(42,{}),(99,{'is_managed':True})])
        async with imported_number_slot(42,db,'+12015550123',post=self.policy(3)) as country:
            self.assertEqual(country,'US')
        with self.assertRaises(ManagedNumberPolicyError):
            async with imported_number_slot(42,db,'+12015550123',post=self.policy(2)):self.fail('Metadata bypassed cap')

    async def test_us_country_is_derived_from_the_number_not_a_supplied_label(self):
        for number,hint in [('+14165550123','US'),('+442079460123','US'),('sip:alice@example.com','US'),('1234','US'),('+12015550123','GB')]:
            db=self.db()
            with self.assertRaises(ManagedNumberPolicyError):
                async with imported_number_slot(42,db,number,hint,post=self.policy()):self.fail('Unsupported geography reached provider work')
            self.assertEqual(db.events,[])

    async def test_us_local_and_toll_free_are_admitted_without_creating_billing_records(self):
        for number in ['+12015550123','+18005550100']:
            db=self.db()
            async with imported_number_slot(42,db,number,post=self.policy()) as country:self.assertEqual(country,'US')
            self.assertEqual(db.connection.execute('SELECT count(*) FROM telephony_phone_numbers').fetchone()[0],0)

    async def test_trusted_paid_context_wins_over_a_stale_native_plan_label(self):
        db=self.db([(42,{})],plan='launch')
        with self.assertRaises(ManagedNumberPolicyError):
            async with imported_number_slot(42,db,'+12015550123',post=self.policy()):self.fail('Native legacy label bypassed paid context')

    async def test_legacy_pstn_and_sip_behavior_is_preserved(self):
        async def legacy(_):return {'applicable':False,'allowed':True}
        db=self.db(plan='launch')
        async with imported_number_slot(42,db,'sip:alice@example.com',post=legacy) as country:self.assertIsNone(country)
        self.assertEqual(db.events,[])

    async def test_actual_create_route_authorizes_config_before_applying_capacity(self):
        db=self.db([(42,{})]);events=[]
        async def own(config_id,org):events.append(('ownership',config_id,org));return object()
        async def persist(config_id,request,user,cfg):
            self.assertNotIn('session-exit',db.events)
            events.append(('persist',request.country_code));return 'created'
        def slot(org,client,address,country):return imported_number_slot(org,client,address,country,post=self.policy())
        class HTTPError(Exception):
            def __init__(self,status_code,detail):self.status_code=status_code;self.detail=detail
        path=Path(__file__).parents[1]/'routes/organization.py'
        fn=next(node for node in ast.parse(path.read_text()).body if isinstance(node,ast.AsyncFunctionDef) and node.name=='create_phone_number');fn.decorator_list=[]
        namespace={'PhoneNumberCreateRequest':object,'UserModel':object,'Depends':lambda *_:None,'get_user':lambda:None,
            'db_client':db,'_ensure_config_belongs_to_org':own,'_create_phone_number':persist,'imported_number_slot':slot,
            'ManagedNumberPolicyError':ManagedNumberPolicyError,'HTTPException':HTTPError}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),str(path),'exec'),namespace)
        with self.assertRaises(HTTPError) as error:
            await namespace['create_phone_number'](7,SimpleNamespace(address='+12015550123',country_code=None),SimpleNamespace(selected_organization_id=42))
        self.assertEqual(error.exception.status_code,403);self.assertEqual(events,[('ownership',7,42)])
        db.connection.execute('DELETE FROM telephony_phone_numbers');db.events.clear()
        class Request:
            address='+12015550123';country_code=None
            def model_copy(self,update):return SimpleNamespace(address=self.address,**update)
        result=await namespace['create_phone_number'](7,Request(),SimpleNamespace(selected_organization_id=42))
        self.assertEqual(result,'created');self.assertEqual(events[-1],('persist','US'))
        self.assertIn('session-exit',db.events)

    async def test_legacy_configuration_route_cannot_import_beyond_the_same_cap(self):
        db=self.db();created=[]
        row=SimpleNamespace(id=7,provider='twilio',credentials={})
        async def default(*_):return row
        async def update(**_):return row
        async def existing(*_):return []
        async def create(**values):
            created.append(values);db.connection.execute('INSERT INTO telephony_phone_numbers VALUES(?,?)',(42,'{}'))
        db.get_default_telephony_configuration=default;db.update_telephony_configuration=update
        db.list_phone_numbers_for_config=existing;db.create_phone_number=create
        def slot(org,client,address):return imported_number_slot(org,client,address,post=self.policy())
        class HTTPError(Exception):
            def __init__(self,status_code,detail):self.status_code=status_code;self.detail=detail
        path=Path(__file__).parents[1]/'routes/organization.py'
        fn=next(node for node in ast.parse(path.read_text()).body if isinstance(node,ast.AsyncFunctionDef) and node.name=='save_telephony_configuration');fn.decorator_list=[]
        namespace={'TelephonyConfigRequest':object,'UserModel':object,'Depends':lambda *_:None,'get_user':lambda:None,
          'db_client':db,'imported_number_slot':slot,'ManagedNumberPolicyError':ManagedNumberPolicyError,'HTTPException':HTTPError,
          'preserve_masked_fields':lambda *_:None,'IntegrityError':type('IntegrityError',(Exception,),{}),'capture_event':lambda **_:None,
          'PostHogEvent':SimpleNamespace(TELEPHONY_CONFIGURED='configured')}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),str(path),'exec'),namespace)
        request=SimpleNamespace(provider='twilio',model_dump=lambda:{'provider':'twilio','from_numbers':['+12015550123','+12015550124']})
        with self.assertRaises(HTTPError) as error:await namespace['save_telephony_configuration'](request,SimpleNamespace(selected_organization_id=42,provider_id='synthetic'))
        self.assertEqual(error.exception.status_code,403);self.assertEqual(len(created),1);self.assertEqual(created[0]['country_code'],'US')

if __name__=='__main__':unittest.main()
