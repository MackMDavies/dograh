"""Contract tests for actual provider source without loading production DB/env.
uv run --no-project --with fastapi==0.135.3 --with aiohttp --with twilio==9.8.0 python api/tests/signalwire_provider_isolated.py
"""
import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree import ElementTree
from twilio.request_validator import RequestValidator

ROOT=Path(__file__).resolve().parents[1]
PACKAGE='api.services.telephony.providers.signalwire'
# The real abstract provider contract is independent of application/DB settings.
for name in ['api','api.services','api.services.telephony','api.services.telephony.providers',PACKAGE,'api.utils']:
 module=types.ModuleType(name);module.__path__=[];sys.modules[name]=module

def module(name,path):
 spec=importlib.util.spec_from_file_location(name,ROOT/path);obj=importlib.util.module_from_spec(spec);sys.modules[name]=obj;spec.loader.exec_module(obj);return obj

module('api.services.telephony.base','services/telephony/base.py')
common=types.ModuleType('api.utils.common');common.get_backend_endpoints=AsyncMock(return_value=('https://api.test','wss://api.test'));sys.modules[common.__name__]=common
address=types.ModuleType('api.utils.telephony_address');address.normalize_telephony_address=lambda value:SimpleNamespace(canonical=value);sys.modules[address.__name__]=address
config=module(PACKAGE+'.config','services/telephony/providers/signalwire/config.py')
provider=module(PACKAGE+'.provider','services/telephony/providers/signalwire/provider.py')
CREDS={'space_url':'customer.signalwire.com','project_id':'12345678-1234-1234-1234-123456789012','api_token':'fixture-api-token','signing_key':'fixture-signing-key','from_numbers':['+12125550101']}

class ConfigTests(unittest.TestCase):
 def test_space_normalization_and_secret_fields(self):
  result=config.SignalWireConfigurationRequest(**{**CREDS,'space_url':'https://Customer.signalwire.com/'})
  self.assertEqual(result.space_url,'customer.signalwire.com')
  with self.assertRaises(ValueError):config.SignalWireConfigurationRequest(**{k:v for k,v in CREDS.items() if k!='signing_key'})
 def test_rejects_credential_exfiltration_hosts(self):
  for host in ['localhost','customer.signalwire.com.evil.test','https://customer.signalwire.com@evil.test','http://customer.signalwire.com','https://customer.signalwire.com:8443','https://customer.signalwire.com/a','customer.signalwire.com?x=1']:
   with self.subTest(host=host),self.assertRaises(ValueError):config.SignalWireConfigurationRequest(**{**CREDS,'space_url':host})

class ProviderTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):self.provider=provider.SignalWireProvider(CREDS)
 async def test_signatures_require_the_signing_key_and_preserve_url_query(self):
  url='https://api.test/callback?workflow_run_id=42';data={'AccountSid':CREDS['project_id'],'CallSid':'call-1'}
  signature=RequestValidator(CREDS['signing_key']).compute_signature(url,data)
  self.assertTrue(await self.provider.verify_inbound_signature(url,data,{'X-SignalWire-Signature':signature}))
  for headers in [{},{'x-signalwire-signature':'bad'},{'x-signalwire-signature':RequestValidator(CREDS['api_token']).compute_signature(url,data)}]:
   self.assertFalse(await self.provider.verify_inbound_signature(url,data,headers))
 async def test_stream_is_bidirectional_secure_and_uses_correct_codec(self):
  xml=ElementTree.fromstring(await self.provider.get_webhook_response(1,2,3));stream=xml.find('Connect/Stream')
  self.assertEqual(stream.attrib,{'url':'wss://api.test/api/v1/telephony/ws/1/2/3','codec':'PCMU@8000h'})
  with self.assertRaises(ValueError):self.provider._stream_xml('ws://api.test/socket')
  with self.assertRaises(ValueError):self.provider._stream_xml('wss://api.test/socket?secret=x')
 async def test_number_discovery_never_modifies_routing(self):
  self.provider._request=AsyncMock(return_value={'incoming_phone_numbers':[{'phone_number':'+12125550101'}]})
  self.assertEqual(await self.provider.get_available_phone_numbers(),['+12125550101'])
  self.assertEqual(self.provider._request.await_args.args[0],'GET')
 async def test_pagination_cannot_send_credentials_to_another_host(self):
  self.provider._request=AsyncMock(return_value={'incoming_phone_numbers':[],'next_page_uri':'https://evil.test/numbers'})
  with self.assertRaises(Exception):await self.provider.list_numbers()
  self.assertEqual(self.provider._request.await_count,1)
 async def test_binding_requires_the_selected_account_number(self):
  self.provider.list_numbers=AsyncMock(return_value=[{'sid':'PN1','phone_number':'+12125550101'}]);self.provider._request=AsyncMock(return_value={'voice_url':'https://api.test/inbound'})
  missing=await self.provider.configure_inbound('+12125550102','https://api.test/inbound');self.assertFalse(missing.ok);self.provider._request.assert_not_awaited()
  bound=await self.provider.configure_inbound('+12125550101','https://api.test/inbound');self.assertTrue(bound.ok)
  self.assertEqual(self.provider._request.await_args.kwargs['data'],{'VoiceUrl':'https://api.test/inbound','VoiceMethod':'POST'})
 async def test_a_rejected_or_unconfirmed_bind_does_not_report_success(self):
  self.provider.list_numbers=AsyncMock(return_value=[{'sid':'PN1','phone_number':'+12125550101'}]);self.provider._request=AsyncMock(return_value={'voice_url':'https://old.test'})
  self.assertFalse((await self.provider.configure_inbound('+12125550101','https://api.test/inbound')).ok)
 async def test_outbound_uses_owned_caller_id_and_provider_callback(self):
  self.provider._request=AsyncMock(return_value={'sid':'call-1','status':'queued'})
  result=await self.provider.initiate_call('+12125550102','https://api.test/answer',workflow_run_id=42)
  self.assertEqual(result.call_id,'call-1');data=self.provider._request.await_args.kwargs['data']
  self.assertEqual(result.provider_metadata,{'call_id':'call-1'})
  self.assertEqual(data['From'],'+12125550101');self.assertEqual(data['StatusCallback'],'https://api.test/api/v1/telephony/signalwire/status/42')
  with self.assertRaises(ValueError):await self.provider.initiate_call('+12125550102','https://api.test/answer',from_number='+12125550103')

 async def test_only_the_matching_persisted_call_can_start_media(self):
  import json
  db=types.ModuleType('api.db');db.db_client=SimpleNamespace(get_workflow_run_by_id=AsyncMock(return_value=SimpleNamespace(gathered_context={'call_id':'call-1'})));sys.modules['api.db']=db
  pipeline=types.ModuleType('api.services.pipecat.run_pipeline');pipeline.run_pipeline_telephony=AsyncMock();sys.modules[pipeline.__name__]=pipeline
  info={'streamSid':'stream-1','callSid':'call-1','accountSid':CREDS['project_id'],'mediaFormat':{'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1}}
  for override in [{'callSid':'another-call'},{'accountSid':'another-project'},{'mediaFormat':{'encoding':'audio/pcm','sampleRate':16000,'channels':1}}]:
   socket=SimpleNamespace(receive_text=AsyncMock(side_effect=[json.dumps({'event':'connected'}),json.dumps({'event':'start','start':{**info,**override}})]),close=AsyncMock())
   await self.provider.handle_websocket(socket,1,2,3);socket.close.assert_awaited_once();pipeline.run_pipeline_telephony.assert_not_awaited()
  socket=SimpleNamespace(receive_text=AsyncMock(side_effect=[json.dumps({'event':'connected'}),json.dumps({'event':'start','start':info})]),close=AsyncMock())
  await self.provider.handle_websocket(socket,1,2,3);socket.close.assert_not_awaited()
  pipeline.run_pipeline_telephony.assert_awaited_once_with(socket,provider_name='signalwire',workflow_id=1,workflow_run_id=3,user_id=2,call_id='call-1',transport_kwargs={'stream_sid':'stream-1','call_sid':'call-1'})

class RouteTests(unittest.TestCase):
 def setUp(self):
  import ast
  from fastapi import APIRouter,FastAPI,Request,Response,HTTPException
  from fastapi.testclient import TestClient
  self.run=SimpleNamespace(id=3,workflow_id=1,gathered_context={'call_id':'call-1'})
  self.workflow=SimpleNamespace(id=1,organization_id=4,user_id=2)
  self.provider=provider.SignalWireProvider(CREDS)
  self.db=SimpleNamespace(get_workflow_run_by_id=AsyncMock(return_value=self.run),get_workflow_by_id=AsyncMock(return_value=self.workflow))
  self.process=AsyncMock()
  context=dict(router=APIRouter(),HTTPException=HTTPException,Request=Request,Response=Response,db_client=self.db,get_telephony_provider_for_run=AsyncMock(return_value=self.provider),get_backend_endpoints=common.get_backend_endpoints,parse_webhook_request=AsyncMock(),StatusCallbackRequest=lambda **kwargs:SimpleNamespace(**kwargs),_process_status_update=self.process)
  self.parse=context['parse_webhook_request']
  tree=ast.parse((ROOT/'services/telephony/providers/signalwire/routes.py').read_text())
  nodes=[n for n in tree.body if isinstance(n,ast.AsyncFunctionDef)]
  exec(compile(ast.Module(body=nodes,type_ignores=[]),'signalwire/routes.py','exec'),context)
  app=FastAPI();app.include_router(context['router']);self.client=TestClient(app)
 def request(self,path,data,signature=None):
  self.parse.return_value=(data,'')
  signature=signature or RequestValidator(CREDS['signing_key']).compute_signature('https://api.test/api/v1/telephony'+path,data)
  return self.client.post(path,data=data,headers={'X-SignalWire-Signature':signature})
 def test_status_passes_only_the_verified_run_id_to_status_processor(self):
  result=self.request('/signalwire/status/3',{'CallSid':'call-1','AccountSid':CREDS['project_id'],'CallStatus':'completed'})
  self.assertEqual(result.status_code,200);self.assertEqual(self.process.await_args.args[0],3)
 def test_invalid_signature_cannot_mutate_call_state(self):
  result=self.request('/signalwire/status/3',{'CallSid':'call-1','AccountSid':CREDS['project_id']},'invalid')
  self.assertEqual(result.status_code,403);self.process.assert_not_awaited()
 def test_another_call_or_project_cannot_reuse_a_run(self):
  for data in [{'CallSid':'other','AccountSid':CREDS['project_id']},{'CallSid':'call-1','AccountSid':'another-project'}]:
   with self.subTest(data=data):self.assertEqual(self.request('/signalwire/status/3',data).status_code,403)
  self.process.assert_not_awaited()
 def test_signed_answer_requires_the_exact_workflow_org_and_user(self):
  path='/signalwire-xml?workflow_id=1&user_id=2&workflow_run_id=3&organization_id=4'
  result=self.request(path,{'CallSid':'call-1','AccountSid':CREDS['project_id']})
  self.assertEqual(result.status_code,200);self.assertIn('<Connect><Stream',result.text)
  wrong='/signalwire-xml?workflow_id=1&user_id=8&workflow_run_id=3&organization_id=4'
  self.assertEqual(self.request(wrong,{'CallSid':'call-1','AccountSid':CREDS['project_id']}).status_code,403)

if __name__=='__main__':unittest.main()
