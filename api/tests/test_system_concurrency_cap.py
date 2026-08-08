"""No path may authorise more than MAX_SYSTEM_CONCURRENCY simultaneous calls.

Sysevo pushes 100000 as an "unlimited" sentinel for the Command/Enterprise
tiers. Without a clamp, an unlimited tier would authorise far more concurrent
voice pipelines than the host can physically run, and the failure mode is an
OOM kill that drops every live call rather than a clean rejection.

The ceiling has to hold on both read and write, and in every consumer — the
campaign validator, campaign defaults, the dispatcher, and the live-call slot
limiter all previously resolved the limit independently.
"""

from unittest.mock import AsyncMock, patch

import pytest

from api.constants import MAX_SYSTEM_CONCURRENCY
from api.services.org_concurrency import clamp_to_system_max


class _Config:
    def __init__(self, value):
        self.value = {"value": value}


class TestClampToSystemMax:
    def test_unlimited_sentinel_is_capped(self):
        assert clamp_to_system_max(100000) == MAX_SYSTEM_CONCURRENCY

    def test_value_at_the_cap_is_preserved(self):
        assert clamp_to_system_max(MAX_SYSTEM_CONCURRENCY) == MAX_SYSTEM_CONCURRENCY

    def test_values_below_the_cap_pass_through(self):
        assert clamp_to_system_max(20) == 20

    def test_zero_and_negative_floor_at_one(self):
        assert clamp_to_system_max(0) == 1
        assert clamp_to_system_max(-5) == 1

    def test_garbage_falls_back_to_a_usable_number(self):
        assert 1 <= clamp_to_system_max("not-a-number") <= MAX_SYSTEM_CONCURRENCY
        assert 1 <= clamp_to_system_max(None) <= MAX_SYSTEM_CONCURRENCY

    def test_the_cap_is_fifty(self):
        """The agreed system-wide maximum. Raising it is a capacity decision."""
        assert MAX_SYSTEM_CONCURRENCY == 50


class TestOrgLimitResolution:
    @pytest.mark.asyncio
    async def test_unlimited_org_config_is_capped_on_read(self):
        from api.services import org_concurrency

        with patch.object(
            org_concurrency,
            "db_client",
            AsyncMock(get_configuration=AsyncMock(return_value=_Config(100000))),
        ):
            limit = await org_concurrency.get_org_concurrency_limit(1)
        assert limit == MAX_SYSTEM_CONCURRENCY

    @pytest.mark.asyncio
    async def test_configured_value_under_the_cap_is_honoured(self):
        from api.services import org_concurrency

        with patch.object(
            org_concurrency,
            "db_client",
            AsyncMock(get_configuration=AsyncMock(return_value=_Config(25))),
        ):
            limit = await org_concurrency.get_org_concurrency_limit(1)
        assert limit == 25

    @pytest.mark.asyncio
    async def test_lookup_failure_falls_back_instead_of_raising(self):
        """Every live call path depends on this — it must never raise."""
        from api.services import org_concurrency

        with patch.object(
            org_concurrency,
            "db_client",
            AsyncMock(get_configuration=AsyncMock(side_effect=RuntimeError("db down"))),
        ):
            limit = await org_concurrency.get_org_concurrency_limit(1)
        assert 1 <= limit <= MAX_SYSTEM_CONCURRENCY


class TestConsumersShareTheCeiling:
    """Each consumer must route through the shared resolver, not its own read."""

    @pytest.mark.asyncio
    async def test_dispatcher_limit_is_capped(self):
        from api.services.campaign.campaign_call_dispatcher import (
            CampaignCallDispatcher,
        )

        with patch(
            "api.services.campaign.campaign_call_dispatcher.get_org_concurrency_limit",
            AsyncMock(return_value=MAX_SYSTEM_CONCURRENCY),
        ):
            limit = await CampaignCallDispatcher().get_org_concurrent_limit(1)
        assert limit == MAX_SYSTEM_CONCURRENCY

    @pytest.mark.asyncio
    async def test_live_call_limiter_uses_the_shared_resolver(self):
        from api.services.telephony import call_concurrency

        with patch.object(
            call_concurrency,
            "get_org_concurrency_limit",
            AsyncMock(return_value=MAX_SYSTEM_CONCURRENCY),
        ):
            assert await call_concurrency._org_limit(1) == MAX_SYSTEM_CONCURRENCY

    @pytest.mark.asyncio
    async def test_campaign_validator_rejects_above_the_org_limit(self):
        """A campaign may not be created above what the org resolves to."""
        from fastapi import HTTPException

        from api.routes import campaign as campaign_routes

        with (
            patch.object(
                campaign_routes,
                "get_org_concurrency_limit",
                AsyncMock(return_value=MAX_SYSTEM_CONCURRENCY),
            ),
            patch.object(
                campaign_routes, "get_calls_per_number", AsyncMock(return_value=None)
            ),
            patch.object(
                campaign_routes, "_get_from_numbers_count", AsyncMock(return_value=1)
            ),
        ):
            # At the cap: allowed, even with a single caller ID configured.
            await campaign_routes._validate_max_concurrency(
                MAX_SYSTEM_CONCURRENCY, organization_id=1
            )

            with pytest.raises(HTTPException) as exc:
                await campaign_routes._validate_max_concurrency(
                    MAX_SYSTEM_CONCURRENCY + 1, organization_id=1
                )
        assert exc.value.status_code == 400

    def test_request_schema_bounds_match_the_system_cap(self):
        """The Pydantic bound and the runtime ceiling must not drift apart."""
        from api.routes.campaign import CreateCampaignRequest, UpdateCampaignRequest

        for model in (CreateCampaignRequest, UpdateCampaignRequest):
            meta = model.model_fields["max_concurrency"].metadata
            upper = next(getattr(m, "le") for m in meta if hasattr(m, "le"))
            assert upper == MAX_SYSTEM_CONCURRENCY, (
                f"{model.__name__}.max_concurrency upper bound is {upper}, "
                f"expected {MAX_SYSTEM_CONCURRENCY}"
            )
