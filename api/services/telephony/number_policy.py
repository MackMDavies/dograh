"""Owner-saved outbound number preferences. Unconfigured numbers keep legacy behavior.

Only trusted call dispatchers use this module, after validating their telephony
configuration. The configuration's actual organization owns the settings/counter,
including a staff-triggered call using another account's validated configuration.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime, time as datetime_time
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
    business_hours_enabled: bool = False
    business_days: tuple[str, ...] = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri')
    open_time: str = '09:00'
    close_time: str = '18:00'
    after_hours: str = 'vm'
    on_fail: str = 'vm'
    forward_to: str | None = None

    def day_key(self, now: datetime | None = None) -> str:
        try:
            zone = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            zone = ZoneInfo('UTC')
        day = (now or datetime.now(UTC)).astimezone(zone).date().isoformat()
        return f'voice-number-day:{self.organization_id}:{self.number_id}:{day}'

    @staticmethod
    def _clock(value: str) -> datetime_time | None:
        try:
            hour, minute = value.split(':', 1)
            result = datetime_time(int(hour), int(minute))
            if len(hour) != 2 or len(minute) != 2:
                return None
            return result
        except (AttributeError, TypeError, ValueError):
            return None

    def is_business_open(self, now: datetime | None = None) -> bool:
        """Evaluate saved hours in the number's IANA timezone, including DST.

        For overnight hours, the selected day is the day on which the window
        opens (e.g. Friday 22:00–02:00 also includes Saturday 01:00).
        Invalid saved configuration is treated as open so a bad preference
        cannot silently block calls.
        """
        if not self.business_hours_enabled:
            return True
        try:
            zone = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            zone = ZoneInfo('UTC')
        local_now = (now or datetime.now(UTC)).astimezone(zone)
        opening = self._clock(self.open_time)
        closing = self._clock(self.close_time)
        days = set(self.business_days)
        weekday = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
        if opening is None or closing is None or not days.issubset(weekday):
            return True
        if not days:
            return False
        if opening == closing:
            return weekday[local_now.weekday()] in days
        current = local_now.timetz().replace(tzinfo=None)
        today = weekday[local_now.weekday()]
        if opening < closing:
            return today in days and opening <= current < closing
        if current >= opening:
            return today in days
        previous_day = weekday[(local_now.weekday() - 1) % 7]
        return current < closing and previous_day in days


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
                'select': ('dograh_phone_number_id,e164,daily_call_cap,local_presence,timezone,'
                           'business_hours_enabled,business_days,open_time,close_time,after_hours,on_fail,forward_to'),
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
                row.get('timezone') or 'UTC',
                row.get('business_hours_enabled') is True,
                tuple(row.get('business_days') or ()),
                row.get('open_time') or '09:00', row.get('close_time') or '18:00',
                row.get('after_hours') if row.get('after_hours') in ('agent', 'vm', 'fwd') else 'vm',
                row.get('on_fail') if row.get('on_fail') in ('vm', 'fwd') else 'vm',
                row.get('forward_to') if isinstance(row.get('forward_to'), str) else None)
        if len(_cache) >= 128:
            _cache.pop(next(iter(_cache)))
        _cache[key] = (time.monotonic() + _CACHE_SECONDS, policies)
        return policies
    except Exception:
        # A failed settings read never erases the last known limit. Existing
        # unconfigured deployments keep their current call path on a cold miss.
        logger.warning('Number preferences unavailable; retaining last known policy')
        return cached[1] if cached else {}


async def load_inbound_number_policy(
    organization_id: int,
    configuration_id: int,
    number_id: int,
    e164: str,
) -> NumberPolicy | None:
    """Return preferences only when both Dograh number identity fields match.

    Inbound dispatch already resolved and signature-validated this number.
    Rechecking its numeric ID and exact canonical address prevents settings
    for a recycled or foreign number from changing call handling.
    """
    policies = await load_number_policies(organization_id, configuration_id)
    policy = policies.get(e164)
    if policy and policy.number_id == number_id and policy.number == e164:
        return policy
    return None


def inbound_hours_action(policy: NumberPolicy | None, now: datetime | None = None) -> str:
    """Resolve the post-signature inbound action for a matched phone number."""
    if policy is None or policy.is_business_open(now):
        return 'agent'
    return policy.after_hours if policy.after_hours in ('agent', 'vm', 'fwd') else 'vm'


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
