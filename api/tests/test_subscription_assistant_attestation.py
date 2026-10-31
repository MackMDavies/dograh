"""A customer workflow flag cannot grant the platform assistant's billing exemption."""
import ast
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from api.services.subscription_call_budget import CallBudgetClient, CallBudgetError
from api.services.syra_voice import is_syra_voice_workflow, is_syra_voice_run


class AssistantTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, registered=False, flag=True):
        calls = []
        async def workflow(identity):
            return SimpleNamespace(id=identity, workflow_configurations={'syra_voice':flag})
        async def post(body):
            calls.append(body)
            return {'assistant':registered}
        return SimpleNamespace(get_workflow_by_id=workflow), CallBudgetClient('https://example.invalid','synthetic',post=post), calls

    async def test_registered_default_and_alternate_assistants_keep_the_exemption(self):
        for identity in (100,101,102):
            db,client,calls=self.fixture(True)
            self.assertTrue(await is_syra_voice_workflow(identity,db=db,client=client))
            self.assertEqual(calls,[{'operation':'assistant','workflow_id':identity}])

    async def test_customer_flag_and_a_cloned_assistant_are_not_exempt(self):
        db,client,calls=self.fixture(False)
        self.assertFalse(await is_syra_voice_workflow(999,db=db,client=client))
        self.assertEqual(len(calls),1)

    async def test_unmarked_workflow_never_requests_an_exemption(self):
        db,client,calls=self.fixture(True,False)
        self.assertFalse(await is_syra_voice_workflow(100,db=db,client=client))
        self.assertEqual(calls,[])

    async def test_invalid_identities_and_mismatched_lookup_are_not_exempt(self):
        db,client,calls=self.fixture(True)
        for identity in (None,True,0,-1,'100',2147483648):
            self.assertFalse(await is_syra_voice_workflow(identity,db=db,client=client))
        async def wrong(_):return SimpleNamespace(id=999,workflow_configurations={'syra_voice':True})
        db.get_workflow_by_id=wrong
        self.assertFalse(await is_syra_voice_workflow(100,db=db,client=client))
        self.assertEqual(calls,[])

    async def test_transport_and_malformed_attestation_never_exempt_a_call(self):
        db,_,_=self.fixture(True)
        for reply in ({},None,{'assistant':'true'},{'assistant':1},{'allowed':True}):
            async def post(_):return reply
            client=CallBudgetClient('https://example.invalid','synthetic',post=post)
            with self.assertRaises(CallBudgetError):await client.attest_assistant(100)
            self.assertFalse(await is_syra_voice_workflow(100,db=db,client=client))
        async def unavailable(_):raise RuntimeError('private transport error')
        client=CallBudgetClient('https://example.invalid','synthetic',post=unavailable)
        self.assertFalse(await is_syra_voice_workflow(100,db=db,client=client))

    async def test_worker_completion_uses_the_same_authoritative_attestation(self):
        for registered in (False,True):
            db,client,calls=self.fixture(registered)
            async def run(_):return SimpleNamespace(workflow_id=100)
            db.get_workflow_run=run
            module=types.ModuleType('api.db');module.db_client=db
            with patch.dict(sys.modules,{'api.db':module}), patch('api.services.subscription_call_budget.configured_budget_client',return_value=client):
                self.assertEqual(await is_syra_voice_run(200),registered)
            self.assertEqual(calls,[{'operation':'assistant','workflow_id':100}])

    async def test_actual_pipeline_resolves_exemption_from_the_trusted_helper(self):
        source=Path(__file__).parents[1]/'services/pipecat/run_pipeline.py'
        function=next(n for n in ast.parse(source.read_text()).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_run_pipeline')
        assignment=next(n for n in function.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='is_syra_voice' for t in n.targets))
        calls=[]
        async def attest(identity):calls.append(identity);return False
        ns={'workflow':SimpleNamespace(id=100),'run_configs':{'syra_voice':True},'is_syra_voice_workflow':attest}
        wrapper=ast.AsyncFunctionDef(name='resolve',args=ast.arguments(posonlyargs=[],args=[],kwonlyargs=[],kw_defaults=[],defaults=[]),body=[assignment,ast.Return(value=ast.Name(id='is_syra_voice',ctx=ast.Load()))],decorator_list=[])
        exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),str(source),'exec'),ns)
        self.assertFalse(await ns['resolve']());self.assertEqual(calls,[100])


if __name__=='__main__':unittest.main()
