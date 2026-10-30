"""Execute the actual completion hook with synthetic HTTP/DB dependencies."""
import ast
import math
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest


class CompletionTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, usage, *, marked=True, completed=True, status=200):
        sent = []
        marker = {'account_id': '10000000-0000-4000-8000-000000000001', 'run_id': 101, 'workflow_id': 123}
        run = SimpleNamespace(workflow_id=123, annotations={'subscription_call_budget': marker} if marked else {},
            usage_info=usage, initial_context={}, api_key_id=7, is_completed=completed)
        async def load(_): return run
        async def release(_): pass
        class HTTP:
            def __init__(self, **_): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def post(self, url, *, json, headers):
                sent.append(json)
                return SimpleNamespace(is_success=status==200, status_code=status, text='synthetic',
                    json=lambda: {'ok': status==200, 'amount_cents': 0})
        source = Path(__file__).parents[1]/'services/wallet_webhook.py'
        tree = ast.parse(source.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name=='fire_post_call_wallet_debit')
        namespace = {'os': SimpleNamespace(getenv=lambda key, default=None: 'https://example.invalid/wallet' if key=='SYSEVO_WALLET_DEBIT_URL' else 'synthetic-secret' if key=='SYSEVO_MEMORY_SECRET' else default),
            'math': math, 'Any': Any, '_TIMEOUT': 15, 'db_client': SimpleNamespace(get_workflow_run_by_id=load),
            'release_call_slot': release, 'sandbox_post_call_policy': lambda _: None,
            'httpx': SimpleNamespace(AsyncClient=HTTP, TimeoutException=TimeoutError),
            'logger': SimpleNamespace(**{key: lambda *_: None for key in ['debug','info','warning','error']})}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
        return namespace['fire_post_call_wallet_debit'], sent

    async def test_explicit_zero_usage_is_sent_for_a_completed_reserved_call(self):
        hook, sent = self.fixture({'duration_seconds': 0})
        self.assertTrue(await hook(101))
        self.assertEqual(sent[0]['duration_secs'], 0)
        self.assertEqual(sent[0]['subscription_call_budget']['run_id'], 101)

    async def test_unknown_usage_never_releases_a_reservation(self):
        for usage in [{}, {'duration_seconds': -1}, {'duration_seconds': float('nan')}]:
            hook, sent = self.fixture(usage)
            self.assertFalse(await hook(101))
            self.assertEqual(sent, [])

    async def test_zero_usage_from_a_running_call_is_not_final(self):
        hook, sent = self.fixture({'duration_seconds': 0}, completed=False)
        self.assertFalse(await hook(101))
        self.assertEqual(sent, [])

    async def test_legacy_zero_duration_keeps_its_existing_skip(self):
        hook, sent = self.fixture({'duration_seconds': 0}, marked=False)
        self.assertTrue(await hook(101))
        self.assertEqual(sent, [])

    async def test_positive_reserved_api_call_retains_identity_and_usage(self):
        hook, sent = self.fixture({'duration_seconds': 61})
        self.assertTrue(await hook(101))
        self.assertEqual(sent[0]['duration_secs'], 61)
        self.assertEqual(sent[0]['api_key_id'], 7)
        self.assertEqual(sent[0]['agent_id'], 123)

    async def test_existing_positive_fallback_duration_is_preserved(self):
        for marked in [True, False]:
            hook, sent = self.fixture({'duration_seconds': 0, 'call_duration_seconds': 61}, marked=marked)
            self.assertTrue(await hook(101))
            self.assertEqual(sent[0]['duration_secs'], 61)

    async def test_retryable_settlement_failure_remains_unsettled(self):
        hook, sent = self.fixture({'duration_seconds': 61}, status=503)
        self.assertFalse(await hook(101))
        self.assertEqual(len(sent), 1)


if __name__ == '__main__': unittest.main()
