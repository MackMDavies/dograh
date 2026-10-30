"""Paid managed-number admission before a carrier purchase; native org owns scope."""
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

LOCK_NAMESPACE = 1397736277
LOCK_SQL = "SELECT pg_advisory_xact_lock(CAST(:namespace AS integer), CAST(:organization AS integer))"
COUNT_SQL = "SELECT count(*) FROM telephony_phone_numbers WHERE organization_id=:organization AND extra_metadata->>'is_managed'='true'"


class ManagedNumberPolicyError(RuntimeError):
    def __init__(self, status_code=503, message='Your paid phone-number allowance could not be verified'):
        super().__init__(message)
        self.status_code = status_code
        self.public_message = message


def positive(value):
    return type(value) is int and 0 < value <= 2147483647


async def number_scope(organization_id, db):
    if not positive(organization_id):
        raise ManagedNumberPolicyError()
    try:
        owner = await db.get_organization_by_id(organization_id)
        provider = getattr(owner, 'provider_id', None)
        if not isinstance(provider, str):
            raise ManagedNumberPolicyError()
        if not provider.startswith('supabase_org_acct_'):
            return None
        account = str(UUID(provider.removeprefix('supabase_org_acct_')))
        config = await db.get_configuration(organization_id, 'SYSEVO_PLAN_KEY')
        value = getattr(config, 'value', None)
        if value is not None and (not isinstance(value, dict) or not isinstance(value.get('value'), str)):
            raise ManagedNumberPolicyError()
        plan = value.get('value') if value else None
        return {'account': account, 'organization': organization_id, 'legacy': bool(plan and not plan.startswith('subscription_')), 'self_serve': bool(plan and plan.startswith('subscription_'))}
    except Exception:
        raise ManagedNumberPolicyError() from None


async def request_number_policy(scope, body, *, post=None):
    try:
        payload = {'account_id': scope['account'], **body}
        if post is not None:
            reply = await post(payload)
        else:
            source = os.getenv('SYSEVO_PRE_CALL_CHECK_URL', '')
            secret = os.getenv('SYSEVO_MEMORY_SECRET', '')
            if not source or not secret:
                raise RuntimeError('Configuration unavailable')
            import httpx
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.post(source.rsplit('/', 1)[0]+'/subscription-managed-number', json=payload,
                    headers={'X-Sysevo-Secret': secret, 'Content-Type': 'application/json'})
            response.raise_for_status()
            reply = response.json()
    except Exception:
        if scope['legacy']:
            return {'applicable': False, 'allowed': True}
        raise ManagedNumberPolicyError() from None
    if not isinstance(reply, dict) or type(reply.get('applicable')) is not bool or type(reply.get('allowed')) is not bool:
        raise ManagedNumberPolicyError()
    if not reply['applicable'] and (not reply['allowed'] or scope['self_serve']):
        raise ManagedNumberPolicyError()
    return reply


def admitted_limit(reply):
    if not reply['allowed']:
        if reply.get('reason') == 'subscription_country_not_supported':
            raise ManagedNumberPolicyError(403, 'Self-Serve phone numbers are currently available in the United States')
        raise ManagedNumberPolicyError(403, 'Number setup requires a current paid subscription and an approved phone allowance')
    if not reply['applicable']:
        return None
    if not positive(reply.get('limit')) or type(reply.get('recorded_used')) is not int or reply['recorded_used'] < 0:
        raise ManagedNumberPolicyError()
    return reply['limit']


@dataclass
class ManagedNumberSlot:
    scope: object
    country: str
    used: object = None
    post: object = None

    async def register(self, row):
        if self.scope is None:
            return
        if not positive(row.id) or row.organization_id != self.scope['organization'] or row.country_code != self.country:
            raise ManagedNumberPolicyError()
        reply = await request_number_policy(self.scope, {'operation': 'register', 'country': self.country,
            'organization_id': self.scope['organization'], 'phone_number_id': row.id, 'e164': row.address, 'used': self.used}, post=self.post)
        if not reply['applicable'] or not reply['allowed'] or reply.get('recorded') is not True or type(reply.get('replayed')) is not bool:
            raise ManagedNumberPolicyError()

    async def release(self, phone_id):
        if self.scope is None:
            return
        if not positive(phone_id):
            raise ManagedNumberPolicyError()
        reply = await request_number_policy(self.scope, {'operation': 'release', 'phone_number_id': phone_id}, post=self.post)
        if not reply['applicable'] or not reply['allowed'] or reply.get('released') is not True:
            raise ManagedNumberPolicyError()


@asynccontextmanager
async def managed_number_slot(organization_id, db, country, *, post=None):
    scope = await number_scope(organization_id, db)
    if scope is None:
        yield ManagedNumberSlot(None, country)
        return
    first = await request_number_policy(scope, {'operation': 'context', 'country': country}, post=post)
    if admitted_limit(first) is None:
        yield ManagedNumberSlot(None, country)
        return
    from sqlalchemy import text
    # Hold the org lock through provider work and the separately committed phone
    # row. A second API worker must observe that row before it can buy a number.
    async with db.async_session() as session:
        async with session.begin():
            await session.execute(text(LOCK_SQL), {'namespace': LOCK_NAMESPACE, 'organization': organization_id})
            current = await request_number_policy(scope, {'operation': 'context', 'country': country}, post=post)
            limit = admitted_limit(current)
            if limit is None:
                raise ManagedNumberPolicyError()  # Classification changed while waiting.
            used = max(int((await session.execute(text(COUNT_SQL), {'organization': organization_id})).scalar() or 0), current['recorded_used'])
            if used < 0:
                raise ManagedNumberPolicyError()
            if used >= limit:
                raise ManagedNumberPolicyError(403, 'Your paid phone-number capacity is full. Purchase another number add-on or release a number first')
            yield ManagedNumberSlot(scope, country, used+1, post)


async def cleanup_released_number(organization_id, phone_id, db, *, post=None):
    """Finish an own-account cleanup even if the native row was already removed."""
    scope = await number_scope(organization_id, db)
    if scope is None:
        return False
    reply = await request_number_policy(scope, {'operation': 'context', 'country': 'US'}, post=post)
    if not reply['applicable']:
        return False
    await ManagedNumberSlot(scope, 'US', post=post).release(phone_id)
    return True


def _release_confirmed(provisioner, sid):
    try:
        if provisioner.release_number(sid):
            return True
        # A retry after a lost release response must distinguish an already
        # absent carrier resource from a real failure before freeing capacity.
        provisioner._client.incoming_phone_numbers(sid).fetch()
    except Exception as error:
        return getattr(error, 'status', None) == 404
    return False


async def release_subscription_number(row, provisioner, db, *, post=None):
    scope = await number_scope(row.organization_id, db)
    if scope is None:
        return False
    reply = await request_number_policy(scope, {'operation': 'context', 'country': 'US'}, post=post)
    if not reply['applicable']:
        return False
    sid = (row.extra_metadata or {}).get('managed_twilio_sid')
    if not sid or provisioner is None:
        raise ManagedNumberPolicyError(503, 'The managed number needs provider reconciliation before release')
    import asyncio
    if not await asyncio.to_thread(_release_confirmed, provisioner, sid):
        raise ManagedNumberPolicyError(502, 'The carrier could not confirm release. The number still occupies its paid slot')
    await db.delete_phone_number(row.id, organization_id=row.organization_id)
    await ManagedNumberSlot(scope, row.country_code, post=post).release(row.id)
    return True
