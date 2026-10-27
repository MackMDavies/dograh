"""Owner-saved outbound number preferences. Unconfigured numbers keep legacy behavior.

Only trusted call dispatchers use this module, after validating their telephony
configuration. The configuration's actual organization owns the settings/counter,
including a staff-triggered call using another account's validated configuration.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import phonenumbers
from fastapi import HTTPException
from loguru import logger

from api.constants import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
from api.db import db_client


@dataclass(frozen=True)
class NumberPolicy:
    organization_id: int
    number_id: int
    number: str
    daily_limit: int
    local_presence: bool = False
    timezone: str = 'UTC'

    def day_key(self, now: datetime | None = None) -> str:
        try:
            zone = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            zone = ZoneInfo('UTC')
        day = (now or datetime.now(UTC)).astimezone(zone).date().isoformat()
        return f'voice-number-day:{self.organization_id}:{self.number_id}:{day}'


_cache: dict[tuple[int, int | None], tuple[float, dict[str, NumberPolicy]]] = {}
_CACHE_SECONDS = 15


def account_from_org(provider_id: str | None) -> str | None:
    prefix = 'supabase_org_acct_'
    if not isinstance(provider_id, str) or not provider_id.startswith(prefix):
        return None
    try:
        return str(UUID(provider_id[len(prefix):]))
    except ValueError:
        return None


async def load_number_policies(organization_id: int, configuration_id: int | None) -> dict[str, NumberPolicy]:
    key = (organization_id, configuration_id)
    cached = _cache.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return {}
    try:
        config = (await db_client.get_telephony_configuration(configuration_id) if configuration_id
                  else await db_client.get_default_telephony_configuration(organization_id))
        if not config:
            return {}
        owner = await db_client.get_organization_by_id(config.organization_id)
        account = account_from_org(owner.provider_id if owner else None)
        if not account:
            return {}
        numbers = await db_client.list_phone_numbers_for_config(config.id)
        owned = {n.id: n for n in numbers if n.organization_id == config.organization_id and n.is_active}
        if not owned:
            return {}
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(SUPABASE_URL + '/rest/v1/voice_number_settings', params={
                'select': 'dograh_phone_number_id,e164,daily_call_cap,local_presence,timezone',
                'client_account_id': 'eq.' + account,
                'dograh_phone_number_id': 'in.(' + ','.join(str(n) for n in owned) + ')',
            }, headers={'apikey': SUPABASE_SERVICE_ROLE_KEY, 'Authorization': 'Bearer ' + SUPABASE_SERVICE_ROLE_KEY})
        response.raise_for_status()
        policies = {}
        for row in response.json():
            number = owned.get(row.get('dograh_phone_number_id'))
            limit = row.get('daily_call_cap')
            if (not number or row.get('e164') != number.address_normalized
                    or type(limit) is not int or not 50 <= limit <= 1500):
                continue
            policies[number.address_normalized] = NumberPolicy(config.organization_id, number.id,
                number.address_normalized, limit, row.get('local_presence') is True,
                row.get('timezone') or 'UTC')
        if len(_cache) >= 128:
            _cache.pop(next(iter(_cache)))
        _cache[key] = (time.monotonic() + _CACHE_SECONDS, policies)
        return policies
    except Exception:
        # A failed settings read never erases the last known limit. Existing
        # unconfigured deployments keep their current call path on a cold miss.
        logger.warning('Number preferences unavailable; retaining last known policy')
        return cached[1] if cached else {}


def geographic_area(number: str) -> tuple[int, str] | None:
    try:
        parsed = phonenumbers.parse(number, None)
        size = phonenumbers.length_of_geographical_area_code(parsed)
        if size:
            return parsed.country_code, phonenumbers.national_significant_number(parsed)[:size]
    except (phonenumbers.NumberParseException, TypeError):
        pass
    return None


def pool_preferences(policies: dict[str, NumberPolicy], destination: str) -> dict[str, dict]:
    area = geographic_area(destination)
    return {number: {'key': policy.day_key(), 'limit': policy.daily_limit,
                     'preferred': bool(policy.local_presence and area and geographic_area(number) == area)}
            for number, policy in policies.items()}


async def reserve_daily_attempt(policy: NumberPolicy) -> bool:
    from api.services.campaign.rate_limiter import rate_limiter
    redis = await rate_limiter._get_redis()
    result = await redis.eval('''
        local used = tonumber(redis.call('GET', KEYS[1]) or '0')
        if used >= tonumber(ARGV[1]) then return 0 end
        redis.call('INCR', KEYS[1])
        redis.call('EXPIRE', KEYS[1], 172800)
        return 1
    ''', 1, policy.day_key(), policy.daily_limit)
    return result == 1


async def reserve_outbound_number(organization_id: int, configuration_id: int | None,
                                  destination: str, allowed_numbers: list[str],
                                  explicit_number: str | None = None) -> str | None:
    policies = await load_number_policies(organization_id, configuration_id)
    if not policies:
        return explicit_number
    choices = [explicit_number] if explicit_number else list(dict.fromkeys(allowed_numbers))
    random.shuffle(choices)
    preferences = pool_preferences(policies, destination)
    choices.sort(key=lambda number: not preferences.get(number, {}).get('preferred', False))
    for number in choices:
        policy = policies.get(number)
        if not policy:
            return number
        try:
            if await reserve_daily_attempt(policy):
                return number
        except Exception as exc:
            raise HTTPException(503, 'Number call limit could not be checked; please retry') from exc
    raise HTTPException(429, 'Daily call limit reached for the selected phone number')
