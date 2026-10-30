import asyncio
import time
import unittest
from unittest.mock import patch

from api.services.subscription_call_budget import CallBudget, CallBudgetClient, CallBudgetError, run_with_call_budget

ACCOUNT = '10000000-0000-4000-8000-000000000001'
REPLY = {'allowed': True, 'kind': 'subscription', 'reserved_minutes': 5,
         'remaining_seconds': 299, 'deadline_epoch': 1800000000}


class BudgetClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_reservation_binds_identity_and_subtracts_roundtrip_and_shutdown_margin(self):
        requests = []
        async def post(payload):
            requests.append(payload)
            return REPLY
        ticks = iter([10.0, 11.0])
        budget = await CallBudgetClient('https://example.invalid/budget', 'synthetic-secret', post=post,
            clock=lambda: next(ticks)).reserve(ACCOUNT, 101, 123, 300)
        self.assertEqual(budget.expires_at, 308.0)
        self.assertEqual(requests, [{'operation': 'reserve', 'account_id': ACCOUNT, 'run_id': 101,
            'workflow_id': 123, 'max_duration_seconds': 300}])

    async def test_unavailable_billing_never_becomes_an_unlimited_budget(self):
        async def post(_):
            raise RuntimeError('synthetic-secret and private response')
        with self.assertRaises(CallBudgetError) as error:
            await CallBudgetClient('https://example.invalid/budget', 'synthetic-secret', post=post).reserve(ACCOUNT, 101, 123, 300)
        self.assertNotIn('synthetic-secret', str(error.exception))

    async def test_missing_configuration_never_posts(self):
        async def post(_):
            self.fail('unconfigured service must not send credentials')
        with self.assertRaises(CallBudgetError):
            await CallBudgetClient('', '', post=post).reserve(ACCOUNT, 101, 123, 300)

    async def test_invalid_identity_never_posts(self):
        async def post(_):
            self.fail('invalid identity reached service')
        client = CallBudgetClient('https://example.invalid', 'synthetic-secret', post=post)
        for args in [('foreign', 1, 2, 300), (ACCOUNT, True, 2, 300), (ACCOUNT, 1, 0, 300), (ACCOUNT, 1, 2, 3.5)]:
            with self.subTest(args=args), self.assertRaises(CallBudgetError):
                await client.reserve(*args)

    async def test_zero_duration_and_replayed_settlement_are_supported(self):
        requests = []
        async def post(payload):
            requests.append(payload)
            return {'settled': True, 'already_settled': True, 'billed_minutes': 0, 'unbilled_minutes': 0}
        result = await CallBudgetClient('https://example.invalid', 'synthetic-secret', post=post).settle(ACCOUNT, 101, 0)
        self.assertTrue(result['already_settled'])
        self.assertEqual(requests[0]['duration_seconds'], 0)

    async def test_invalid_settlement_and_unconfirmed_success_are_rejected(self):
        async def post(_):
            return {'settled': False}
        client = CallBudgetClient('https://example.invalid', 'synthetic-secret', post=post)
        for duration in [True, -1, float('inf'), float('nan'), None, '61']:
            with self.subTest(duration=duration), self.assertRaises(CallBudgetError):
                await client.settle(ACCOUNT, 101, duration)
        with self.assertRaises(CallBudgetError):
            await client.settle(ACCOUNT, 101, 61)

    def test_bad_and_replayed_budget_shapes_fail_closed(self):
        for reply in [None, {}, {'allowed': False}, {**REPLY, 'kind': 'legacy'},
                      {**REPLY, 'reserved_minutes': True}, {**REPLY, 'reserved_minutes': 0},
                      {**REPLY, 'remaining_seconds': 301}, {**REPLY, 'remaining_seconds': float('nan')},
                      {**REPLY, 'remaining_seconds': 0}, {**REPLY, 'reserved_minutes': 1},
                      {**REPLY, 'deadline_epoch': -1}]:
            with self.subTest(reply=reply), self.assertRaises(CallBudgetError):
                CallBudget.from_reply(reply, run_id=101, max_seconds=300, sent_at=10, received_at=11)

    def test_network_delay_cannot_restart_a_nearly_expired_reservation(self):
        with self.assertRaises(CallBudgetError):
            CallBudget.from_reply({**REPLY, 'remaining_seconds': 2}, run_id=101, max_seconds=300,
                                  sent_at=10, received_at=12)


class DeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_deadline_stops_audio_without_a_heartbeat_or_llm_response(self):
        stopped = asyncio.Event()
        invoked = []
        async def run():
            await stopped.wait()
        async def stop():
            invoked.append(time.monotonic())
            stopped.set()
        start = time.monotonic()
        await run_with_call_budget(run, stop, CallBudget(101, 1, start+.02), shutdown_grace=.03)
        self.assertEqual(len(invoked), 1)
        self.assertLess(invoked[0]-start, .2)

    async def test_completed_call_removes_the_timer(self):
        invoked = []
        async def run():
            return 'completed'
        async def stop():
            invoked.append(True)
        result = await run_with_call_budget(run, stop, CallBudget(101, 1, time.monotonic()+.02), shutdown_grace=.03)
        await asyncio.sleep(.03)
        self.assertEqual(result, 'completed')
        self.assertEqual(invoked, [])

    async def test_expired_budget_never_starts_audio(self):
        async def forbidden():
            self.fail('expired budget started audio')
        with self.assertRaises(CallBudgetError):
            await run_with_call_budget(forbidden, forbidden, CallBudget(101, 1, time.monotonic()-1))

    async def test_failed_stop_cancels_the_audio_coroutine_and_propagates(self):
        cleaned = asyncio.Event()
        async def run():
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        async def stop():
            raise CallBudgetError('stop failed')
        with self.assertRaises(CallBudgetError):
            await run_with_call_budget(run, stop, CallBudget(101, 1, time.monotonic()+.01), shutdown_grace=.02)
        self.assertTrue(cleaned.is_set())

    async def test_nonterminating_audio_is_cancelled_after_the_shutdown_grace(self):
        cleaned = asyncio.Event()
        async def run():
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        async def stop():
            return None
        await run_with_call_budget(run, stop, CallBudget(101, 1, time.monotonic()+.01), shutdown_grace=.02)
        self.assertTrue(cleaned.is_set())

    async def test_natural_run_error_is_not_hidden_by_the_timer(self):
        async def run():
            raise ValueError('synthetic pipeline error')
        async def stop():
            self.fail('natural error should remove deadline')
        with self.assertRaisesRegex(ValueError, 'synthetic pipeline error'):
            await run_with_call_budget(run, stop, CallBudget(101, 1, time.monotonic()+1))

    async def test_wall_clock_changes_do_not_extend_the_deadline(self):
        stopped = asyncio.Event()
        async def run():
            await stopped.wait()
        async def stop():
            stopped.set()
        with patch('time.time', return_value=-10**20):
            await asyncio.wait_for(run_with_call_budget(run, stop,
                CallBudget(101, 1, time.monotonic()+.01), shutdown_grace=.02), timeout=.5)
        self.assertTrue(stopped.is_set())

    async def test_parent_cancellation_removes_the_deadline_and_audio_tasks(self):
        started, cleaned = asyncio.Event(), asyncio.Event()
        stopped = []
        async def run():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        async def stop():
            stopped.append(True)
        parent = asyncio.create_task(run_with_call_budget(run, stop,
            CallBudget(101, 1, time.monotonic()+10)))
        await started.wait()
        parent.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await parent
        self.assertTrue(cleaned.is_set())
        self.assertEqual(stopped, [])


if __name__ == '__main__':
    unittest.main()
