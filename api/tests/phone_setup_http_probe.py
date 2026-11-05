"""Exercise actual phone handlers through FastAPI without loading database settings.

Run from the repo root:
uv run --no-project --with fastapi==0.135.3 --with httpx==0.28.1 python api/tests/phone_setup_http_probe.py

Provider and database calls are mocks; schema validation and response serialization
use the actual route declarations. This supplements the full pytest route suite.
"""
import ast, asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, Optional
from unittest import TestCase, main
from unittest.mock import AsyncMock, MagicMock
from fastapi import FastAPI, APIRouter, Depends, Query, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

class ProviderError(Exception):
    pass

class HttpContractTests(TestCase):
    def setUp(self):
        provider=MagicMock()
        provider.search_available_numbers_detailed.return_value=[{'phone_number':'+14155550101','region':'CA','capabilities':['Voice']}]
        provider.available_number_types.return_value=['local','toll_free']
        provider.has_inbound_call_since.return_value=True
        db=SimpleNamespace(get_phone_number=AsyncMock(return_value=SimpleNamespace(address='+12125550101',extra_metadata={'is_managed':True})))
        context=dict(ast=ast,asyncio=asyncio,datetime=datetime,timezone=timezone,timedelta=timedelta,Optional=Optional,Literal=Literal,BaseModel=BaseModel,Query=Query,Depends=Depends,HTTPException=HTTPException,UserModel=object,router=APIRouter(prefix='/telephony'),get_user=lambda:SimpleNamespace(selected_organization_id=11),get_managed_provisioner=AsyncMock(return_value=provider),_provisioner_for_number=AsyncMock(return_value=provider),db_client=db,_resolve_platform_account_id=lambda user,platform:None,number_scope=AsyncMock(return_value={'account':'owned'}),request_number_policy=AsyncMock(side_effect=lambda scope,body:{'allowed':body['country']=='US'}),ManagedNumberPolicyError=ProviderError,TwilioRestException=ProviderError)
        tree=ast.parse((Path(__file__).resolve().parents[1]/'routes/telephony_quick_connect.py').read_text())
        names={'AvailableNumberItem','AvailableNumbersResponse','available_numbers','available_number_countries','forwarding_check'}
        nodes=[node for node in tree.body if isinstance(node,(ast.ClassDef,ast.AsyncFunctionDef)) and node.name in names]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'telephony_quick_connect.py','exec'),context)
        app=FastAPI();app.include_router(context['router'])
        self.client=TestClient(app);self.provider=provider;self.db=db
    def test_state_is_passed_and_real_provider_results_are_serialized(self):
        response=self.client.get('/telephony/available-numbers',params={'country':'US','in_region':'ca','number_type':'local','detailed':'true'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['items'][0]['region'],'CA')
        self.provider.search_available_numbers_detailed.assert_called_once_with('US','local',None,None,None,in_region='CA')
    def test_invalid_state_is_rejected_by_the_http_schema(self):
        response=self.client.get('/telephony/available-numbers',params={'country':'US','in_region':'bad!','number_type':'local'})
        self.assertEqual(response.status_code,422)
        self.provider.search_available_numbers_detailed.assert_not_called()
    def test_provider_failure_is_a_readable_http_error(self):
        self.provider.search_available_numbers_detailed.side_effect=ProviderError()
        response=self.client.get('/telephony/available-numbers',params={'country':'US','number_type':'toll_free'})
        self.assertEqual(response.status_code,502)
        self.assertIn('could not be checked',response.json()['detail'])
    def test_country_types_follow_the_paid_policy(self):
        response=self.client.get('/telephony/available-number-countries')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),{'countries':[{'code':'US','name':'United States','types':['local','toll_free']}]})
    def test_forwarding_observation_is_organization_scoped(self):
        response=self.client.get('/telephony/forwarding-check',params={'phone_number_id':42,'since':datetime.now(timezone.utc).isoformat()})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),{'observed':True})
        self.db.get_phone_number.assert_awaited_once_with(42,organization_id=11)
    def test_forwarding_rejects_another_tenants_destination(self):
        self.db.get_phone_number.return_value=None
        response=self.client.get('/telephony/forwarding-check',params={'phone_number_id':42,'since':datetime.now(timezone.utc).isoformat()})
        self.assertEqual(response.status_code,404)
        self.provider.has_inbound_call_since.assert_not_called()
    def test_forwarding_rejects_stale_and_naive_start_times(self):
        for since in [(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat(),datetime.now().isoformat()]:
            response=self.client.get('/telephony/forwarding-check',params={'phone_number_id':42,'since':since})
            self.assertEqual(response.status_code,422)
        self.db.get_phone_number.assert_not_called()

if __name__=='__main__':main()
