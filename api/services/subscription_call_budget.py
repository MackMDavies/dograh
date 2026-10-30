"""Self-Serve budget client and independent deadline; legacy callers opt out.

The caller must resolve the account from the workflow's persisted organization,
classify billing from trusted state, and skip attested nonbillable sandboxes.
This module never treats an error as permission to start unrestricted audio.
"""
import asyncio
import math
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable
from uuid import UUID


class CallBudgetError(RuntimeError):
    pass


def _positive_int(value):
    return type(value) is int and 0 < value <= 2147483647


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True)
class CallBudget:
    run_id: int
    reserved_minutes: int
    expires_at: float  # monotonic, deliberately independent of wall-clock time

    @classmethod
    def from_reply(cls, reply, *, run_id, max_seconds, sent_at, received_at):
        if (not isinstance(reply, dict) or reply.get('allowed') is not True
                or reply.get('kind') != 'subscription' or not _positive_int(run_id)
                or not _positive_int(max_seconds) or not _positive_int(reply.get('reserved_minutes'))
                or not _number(reply.get('deadline_epoch')) or reply['deadline_epoch'] <= 0
                or not _number(reply.get('remaining_seconds'))
                or not 0 < reply['remaining_seconds'] <= min(max_seconds, reply['reserved_minutes'] * 60)
                or not _number(sent_at) or not _number(received_at) or received_at < sent_at):
            raise CallBudgetError('Call budget could not be verified')
        # Subtract the entire round trip, plus a shutdown margin. Replaying an
        # already-started reservation cannot create a fresh full-duration timer.
        expires_at = sent_at + reply['remaining_seconds'] - 1.0
        if expires_at <= received_at:
            raise CallBudgetError('Call budget expired before audio could start')
        return cls(run_id, reply['reserved_minutes'], expires_at)


class CallBudgetClient:
    def __init__(self, endpoint: str, secret: str, *, post=None, clock=time.monotonic):
        self.endpoint = endpoint
        self.secret = secret
        self.post = post
        self.clock = clock

    async def _request(self, payload):
        if not self.endpoint or not self.secret:
            raise CallBudgetError('Call billing is not configured')
        try:
            if self.post is not None:
                return await self.post(payload)
            import httpx
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.post(self.endpoint, json=payload,
                    headers={'X-Sysevo-Secret': self.secret, 'Content-Type': 'application/json'})
            response.raise_for_status()
            return response.json()
        except Exception:
            # Do not include hook credentials, response bodies or HTTP internals
            # in caller-visible errors or later reconciliation logs.
            raise CallBudgetError('Call billing is unavailable') from None

    async def reserve(self, account_id, run_id, workflow_id, max_seconds, *, runtime_configuration=None):
        try:
            account_id = str(UUID(account_id))
        except (ValueError, TypeError, AttributeError):
            raise CallBudgetError('Invalid call account') from None
        if not all(_positive_int(v) for v in (run_id, workflow_id, max_seconds)):
            raise CallBudgetError('Invalid call identity or duration')
        sent_at = self.clock()
        payload = {'operation': 'reserve', 'account_id': account_id,
            'run_id': run_id, 'workflow_id': workflow_id, 'max_duration_seconds': max_seconds}
        if runtime_configuration is not None:
            payload['runtime_configuration'] = runtime_configuration
        reply = await self._request(payload)
        return CallBudget.from_reply(reply, run_id=run_id, max_seconds=max_seconds,
            sent_at=sent_at, received_at=self.clock())

    async def classify(self, account_id, run_id):
        reply = await self._request({'operation': 'classify', 'account_id': account_id, 'run_id': run_id})
        if not isinstance(reply, dict) or reply.get('kind') not in ('subscription', 'legacy'):
            raise CallBudgetError('Call billing route could not be verified')
        return reply['kind']

    async def settle(self, account_id, run_id, duration_seconds):
        try:
            account_id = str(UUID(account_id))
        except (ValueError, TypeError, AttributeError):
            raise CallBudgetError('Invalid call account') from None
        if not _positive_int(run_id) or not _number(duration_seconds) or not 0 <= duration_seconds <= 2147483647:
            raise CallBudgetError('Invalid call settlement')
        reply = await self._request({'operation': 'settle', 'account_id': account_id,
            'run_id': run_id, 'duration_seconds': duration_seconds})
        if (not isinstance(reply, dict) or reply.get('settled') is not True
                or type(reply.get('already_settled')) is not bool
                or type(reply.get('billed_minutes')) is not int or reply['billed_minutes'] < 0
                or type(reply.get('unbilled_minutes')) is not int or reply['unbilled_minutes'] < 0):
            raise CallBudgetError('Call settlement is pending')
        return reply


def configured_budget_client():
    source = os.getenv('SYSEVO_PRE_CALL_CHECK_URL', '')
    endpoint = source.rsplit('/', 1)[0] + '/subscription-call-budget' if source else ''
    return CallBudgetClient(endpoint, os.getenv('SYSEVO_MEMORY_SECRET', ''))


async def prepare_pipeline_call_budget(workflow, workflow_run, max_seconds, db, *, client=None, skip=False, resolved_models=None):
    """Bind the budget to the persisted workflow organization, never a user selection.

    The local plan marker is only an outage fallback for an explicitly known
    legacy organization. A missing marker never proves that a workspace is legacy.
    Normal admission always asks the authoritative platform billing route.
    """
    if skip:
        return None
    if workflow_run is None or workflow_run.workflow_id != workflow.id:
        raise CallBudgetError('Call workflow binding changed')
    owner = await db.get_organization_by_id(workflow.organization_id)
    if owner is None:
        raise CallBudgetError('Call organization could not be verified')
    provider = getattr(owner, 'provider_id', None)
    if not isinstance(provider, str) or not provider.startswith('supabase_org_acct_'):
        return None  # Existing non-Sysevo/per-user OSS organizations.
    try:
        account = str(UUID(provider.removeprefix('supabase_org_acct_')))
    except ValueError:
        raise CallBudgetError('Call account binding is malformed') from None
    config = await db.get_configuration(workflow.organization_id, 'SYSEVO_PLAN_KEY')
    value = getattr(config, 'value', None)
    if value is not None and (not isinstance(value, dict) or not isinstance(value.get('value'), str)):
        raise CallBudgetError('Call plan configuration is malformed')
    local_plan = value.get('value') if value else None
    billing = client or configured_budget_client()
    try:
        route = await billing.classify(account, workflow_run.id)
    except CallBudgetError:
        if local_plan and not local_plan.startswith('subscription_'):
            return None  # Preserve the existing explicit legacy outage behavior.
        raise
    if route == 'legacy':
        if local_plan and local_plan.startswith('subscription_'):
            raise CallBudgetError('Call billing route changed')
        return None
    if route != 'subscription':
        raise CallBudgetError('Call billing route could not be verified')
    budget = await billing.reserve(account, workflow_run.id, workflow.id, max_seconds,
        runtime_configuration=resolved_models)
    await db.update_workflow_run(workflow_run.id, annotations={'subscription_call_budget': {
        'account_id': account, 'run_id': workflow_run.id, 'workflow_id': workflow.id,
    }})
    return budget


async def run_with_call_budget(
    run: Callable[[], Awaitable], stop: Callable[[], Awaitable], budget: CallBudget,
    *, clock=time.monotonic, shutdown_grace=2.0,
):
    """Run audio with a deadline which does not depend on frames or the LLM.

    `stop` must be Pipecat PipelineTask.cancel, not the engine's end-call helper
    which awaits LLM extraction before cancellation. Once cancellation is queued,
    allow bounded cleanup, then cancel the run coroutine as a final backstop.
    """
    if budget.expires_at <= clock():
        raise CallBudgetError('Call budget expired before audio could start')
    running = asyncio.create_task(run())

    async def deadline():
        await asyncio.sleep(max(0, budget.expires_at - clock()))
        await asyncio.wait_for(stop(), timeout=shutdown_grace)
        await asyncio.sleep(shutdown_grace)
        if not running.done():
            running.cancel()

    timer = asyncio.create_task(deadline())
    try:
        done, _ = await asyncio.wait((running, timer), return_when=asyncio.FIRST_COMPLETED)
        if timer in done:
            # Propagate a failed cancellation; never silently leave audio running.
            await timer
            if running.cancelled():
                return None
        return await running
    finally:
        for task in (timer, running):
            if not task.done():
                task.cancel()
        await asyncio.gather(timer, running, return_exceptions=True)
