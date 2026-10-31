"""Execute the actual shared-pipeline dispatch block without provider imports."""
import ast
import asyncio
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
import time
import unittest

from api.services.subscription_call_budget import CallBudget, CallBudgetError, run_with_call_budget

PIPELINE_SOURCE = Path(__file__).parents[1]/'services/pipecat/run_pipeline.py'


class SharedPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, budget=None, fail=False, sandbox=False, assistant_flag=False, attested_assistant=False):
        path = PIPELINE_SOURCE
        function = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.AsyncFunctionDef) and n.name=='_run_pipeline')
        block = function.body[-1]
        self.assertIsInstance(block, ast.Try)
        order = []
        async def prepare(*args, **kwargs):
            order.append(('reserve', kwargs['skip']))
            self.assertEqual(kwargs['resolved_models'], {'is_realtime': True, 'realtime_model': 'eleven_realtime_v1_mini'})
            if fail: raise CallBudgetError('synthetic rejection')
            return budget
        async def run(params): order.append(('audio', None))
        async def cancel(**kwargs): order.append(('cancel', None))
        async def cleanup(): order.append(('cleanup', None))
        async def guarded(*args, **kwargs):
            order.append(('guard', None))
            return await run_with_call_budget(*args, **kwargs)
        namespace = {'asyncio': asyncio, 'prepare_pipeline_call_budget': prepare,
            'run_with_call_budget': guarded, 'workflow': object(), 'workflow_run': object(),
            'max_call_duration_seconds': 300, 'db_client': object(), 'is_agent_test_sandbox': sandbox,
            'is_realtime': True, 'runtime_configuration': {'realtime_model': 'eleven_realtime_v1_mini'},
            'run_configs': {'syra_voice': assistant_flag}, 'is_syra_voice': attested_assistant, 'PipelineTaskParams': lambda **kwargs: kwargs,
            'task': SimpleNamespace(run=run, cancel=cancel), 'supervisor': None, 'suppress': suppress,
            'engine': SimpleNamespace(stop_companion=cleanup),
            'feedback_observer': SimpleNamespace(cleanup=cleanup), 'workflow_run_id': 101,
            'logger': SimpleNamespace(info=lambda *_: None, warning=lambda *_: None, debug=lambda *_: None)}
        wrapper = ast.AsyncFunctionDef(name='exercise', args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
            body=[block], decorator_list=[])
        exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), str(path), 'exec'), namespace)
        try:
            await namespace['exercise']()
        except CallBudgetError:
            self.assertTrue(fail)
        return order

    async def test_self_serve_reserves_then_runs_under_the_deadline(self):
        order = await self.exercise(budget=CallBudget(101, 5, time.monotonic()+1))
        self.assertEqual(order[:3], [('reserve', False), ('guard', None), ('audio', None)])

    async def test_rejected_budget_never_starts_audio_and_still_cleans_up(self):
        order = await self.exercise(fail=True)
        self.assertNotIn(('audio', None), order)
        self.assertIn(('cleanup', None), order)

    async def test_existing_legacy_dispatch_remains_direct(self):
        order = await self.exercise()
        self.assertEqual(order[:2], [('reserve', False), ('audio', None)])
        self.assertNotIn(('guard', None), order)

    async def test_customer_workflow_cannot_claim_an_assistant_budget_exemption(self):
        order = await self.exercise(assistant_flag=True)
        self.assertEqual(order[0], ('reserve', False))

    async def test_registered_assistant_keeps_its_separate_nonbillable_path(self):
        order = await self.exercise(assistant_flag=True, attested_assistant=True)
        self.assertEqual(order[0], ('reserve', True))

    async def test_attested_sandbox_bypass_is_passed_to_the_service(self):
        order = await self.exercise(sandbox=True)
        self.assertEqual(order[0], ('reserve', True))


if __name__ == '__main__': unittest.main()
