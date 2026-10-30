"""Monthly developer API accounting for authenticated native voice keys.

The persisted API key owns organization scope. Never accept account identity,
request IDs, or plan claims from the caller. Browser JWT traffic does not enter
this service. Legacy/Enterprise postpaid accounting remains the caller's job.
"""
import os
from uuid import UUID, uuid4


class NativeApiMeterError(RuntimeError):
    def __init__(self, status_code=503, message='Subscription API access could not be verified'):
        super().__init__(message)
        self.status_code = status_code
        self.public_message = message


async def meter_native_api_request(key, db, *, post=None, is_mcp=False):
    """Return True only for an admitted Self-Serve request, False for legacy.

    Key validity/expiry and owner existence must already be established by the
    normal native authentication flow. No raw API key is sent to the platform.
    """
    if type(is_mcp) is not bool:
        raise NativeApiMeterError()
    if type(key.id) is not int or not 0 < key.id <= 2147483647 or type(key.organization_id) is not int or key.organization_id <= 0:
        raise NativeApiMeterError()
    try:
        owner = await db.get_organization_by_id(key.organization_id)
    except Exception:
        raise NativeApiMeterError() from None
    provider = getattr(owner, 'provider_id', None)
    if not isinstance(provider, str):
        raise NativeApiMeterError()
    if not provider.startswith('supabase_org_acct_'):
        return False  # Preserve non-Sysevo/OSS API use without a new dependency.
    try:
        account = str(UUID(provider.removeprefix('supabase_org_acct_')))
    except (ValueError, TypeError):
        raise NativeApiMeterError() from None
    try:
        config = await db.get_configuration(key.organization_id, 'SYSEVO_PLAN_KEY')
    except Exception:
        raise NativeApiMeterError() from None
    marker = getattr(config, 'value', None)
    if marker is not None and (not isinstance(marker, dict) or not isinstance(marker.get('value'), str)):
        raise NativeApiMeterError()
    local_plan = marker.get('value') if marker else None
    known_legacy = bool(local_plan and not local_plan.startswith('subscription_'))
    payload = {'account_id': account, 'native_api_key_id': key.id, 'request_id': str(uuid4()), 'is_mcp': is_mcp}
    try:
        if post is not None:
            reply = await post(payload)
        else:
            source = os.getenv('SYSEVO_PRE_CALL_CHECK_URL', '')
            secret = os.getenv('SYSEVO_MEMORY_SECRET', '')
            if not source or not secret:
                raise RuntimeError('Configuration unavailable')
            import httpx
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.post(source.rsplit('/', 1)[0] + '/subscription-api-meter',
                    json=payload, headers={'X-Sysevo-Secret': secret, 'Content-Type': 'application/json'})
            response.raise_for_status()
            reply = response.json()
    except Exception:
        if known_legacy:
            return False  # Preserve explicit Enterprise outage behavior.
        raise NativeApiMeterError() from None
    if not isinstance(reply, dict) or type(reply.get('applicable')) is not bool or type(reply.get('allowed')) is not bool:
        raise NativeApiMeterError()
    if not reply['applicable']:
        if not reply['allowed'] or (local_plan and local_plan.startswith('subscription_')):
            raise NativeApiMeterError()
        return False
    if not reply['allowed']:
        if reply.get('reason') == 'monthly_request_allowance_reached':
            raise NativeApiMeterError(429, 'The monthly API and MCP request allowance has been reached')
        raise NativeApiMeterError(403, 'API access is not enabled for this subscription')
    return True
