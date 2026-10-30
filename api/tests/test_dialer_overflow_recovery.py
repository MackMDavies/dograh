"""Recovery worker orchestration. Provider calls are mocked; none are placed."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services.telephony.providers.twilio import dialer_overflow_recovery as recovery

MOD = "api.services.telephony.providers.twilio.dialer_overflow_recovery"


@pytest.fixture
def deps():
    job = {"child_call_sid":"child","parent_call_sid":"parent","leg_id":"leg","account_sid":"account","lease_token":"token"}
    rpc = AsyncMock(side_effect=lambda name,body: [job] if name=="dialer_claim_parallel_overflow_jobs" else True)
    client = MagicMock(account_sid="account")
    client.calls.return_value.fetch.return_value = SimpleNamespace(status="in-progress")
    prepared = SimpleNamespace(twiml="stream",already_streaming=False)
    with (
        patch(f"{MOD}.dialer_agent_handoff._rpc",rpc),
        patch(f"{MOD}._twilio_client",new=AsyncMock(return_value=client)),
        patch(f"{MOD}.dialer_agent_handoff.prepare_agent_handoff",new=AsyncMock(return_value=prepared)) as prepare,
        patch(f"{MOD}.cancel_call",new=AsyncMock(return_value=True)) as cancel,
    ):
        yield SimpleNamespace(job=job,rpc=rpc,client=client,prepared=prepared,prepare=prepare,cancel=cancel)


async def test_due_offer_updates_exact_existing_call_then_acknowledges_lease(deps):
    d = deps
    await recovery.recover_overflow_offers(child_call_sid="child")
    d.prepare.assert_awaited_once_with(parent_call_sid="parent",child_call_sid="child",leg_id="leg",account_sid="account",allow_fallback=True)
    d.client.calls.assert_any_call("child")
    d.client.calls.return_value.update.assert_called_once_with(twiml="stream")
    assert d.rpc.await_args.args == ("dialer_finish_parallel_overflow_job",{"p_child_call_sid":"child","p_lease_token":"token"})
    d.client.calls.create.assert_not_called()


async def test_recovery_leaves_an_already_authenticated_agent_running(deps):
    deps.prepared.already_streaming = True
    await recovery.recover_overflow_offers()
    deps.client.calls.return_value.update.assert_not_called()
    assert deps.rpc.await_count == 2


async def test_lost_provider_acknowledgement_keeps_job_recoverable(deps):
    deps.client.calls.return_value.update.side_effect = TimeoutError()
    await recovery.recover_overflow_offers()
    assert deps.rpc.await_count == 1
    deps.cancel.assert_not_awaited()


async def test_terminal_original_call_never_starts_agent(deps):
    deps.client.calls.return_value.fetch.return_value.status = "completed"
    await recovery.recover_overflow_offers()
    deps.prepare.assert_not_awaited()
    assert deps.rpc.await_count == 2


async def test_missing_agent_ends_original_line_before_acknowledging(deps):
    deps.prepare.return_value = None
    await recovery.recover_overflow_offers()
    deps.cancel.assert_awaited_once_with(call_sid="child")
    assert deps.rpc.await_count == 2


async def test_failed_cancellation_remains_retryable(deps):
    deps.prepare.return_value = None
    deps.cancel.return_value = False
    await recovery.recover_overflow_offers()
    assert deps.rpc.await_count == 1


async def test_incompatible_agent_ends_the_existing_line_without_waiting_forever(deps):
    deps.prepare.side_effect = recovery.dialer_agent_handoff.AgentHandoffUnavailable("Wrong provider account")
    await recovery.recover_overflow_offers()
    deps.cancel.assert_awaited_once_with(call_sid="child")
    assert deps.rpc.await_count == 2


async def test_temporary_storage_failure_never_means_permission_to_cancel(deps):
    deps.prepare.side_effect = recovery.dialer_agent_handoff.AgentHandoffUnavailable("Storage unavailable",retryable=True)
    await recovery.recover_overflow_offers()
    deps.cancel.assert_not_awaited()
    assert deps.rpc.await_count == 1


async def test_changed_provider_account_never_touches_original_call(deps):
    deps.client.account_sid = "another-account"
    await recovery.recover_overflow_offers()
    deps.client.calls.assert_not_called()
    deps.prepare.assert_not_awaited()
    assert deps.rpc.await_count == 1


async def test_claimed_invitation_is_not_returned_to_the_worker(deps):
    deps.rpc.side_effect = None
    deps.rpc.return_value = []
    await recovery.recover_overflow_offers()
    deps.client.calls.assert_not_called()


async def test_scheduling_is_durable_before_the_timer_starts(deps):
    assert await recovery.schedule_overflow(parent_call_sid="parent",child_call_sid="child",leg_id="leg",account_sid="account")
    deps.rpc.assert_awaited_once_with("dialer_schedule_parallel_overflow",{
        "p_parent_call_sid":"parent","p_child_call_sid":"child","p_leg_id":"leg","p_account_sid":"account",
    })
