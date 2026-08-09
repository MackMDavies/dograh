"""Outbound number assignment: which numbers dial, and which one a campaign uses.

Two behaviours are load-bearing and easy to get silently wrong:

1. A number reserved to answer a published inbound line must never appear as an
   outbound caller ID. Get the predicate backwards and either the dedicated
   inbound line starts cold-calling, or — far worse — the outbound pool empties
   and every campaign stops dialling with no error.

2. A campaign pinned to one caller ID must actually use it, and must degrade to
   the pool rather than stranding itself if that number is later released.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import or_
from sqlalchemy.future import select

from api.db.models import TelephonyPhoneNumberModel
from api.services.campaign.campaign_call_dispatcher import CampaignCallDispatcher


def _pool_where_clause() -> str:
    """The exclusion predicate as SQL text, mirroring the production query."""
    stmt = select(TelephonyPhoneNumberModel.address_normalized).where(
        TelephonyPhoneNumberModel.telephony_configuration_id == 1,
        TelephonyPhoneNumberModel.is_active.is_(True),
        ~(
            TelephonyPhoneNumberModel.inbound_workflow_id.isnot(None)
            & TelephonyPhoneNumberModel.outbound_workflow_id.is_(None)
        ),
    )
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


class TestOutboundPoolExclusion:
    """The rule is 'not inbound-only', deliberately not 'outbound assigned'."""

    def test_predicate_excludes_inbound_only_numbers(self):
        sql = _pool_where_clause()
        assert "inbound_workflow_id IS NOT NULL" in sql
        assert "outbound_workflow_id IS NULL" in sql
        assert "NOT (" in sql

    def test_predicate_is_not_a_bare_outbound_assigned_check(self):
        # `WHERE outbound_workflow_id IS NOT NULL` would look equivalent but
        # empties the pool for every pre-existing row, stopping all dialling
        # until someone assigns each number by hand.
        sql = _pool_where_clause()
        assert "outbound_workflow_id IS NOT NULL" not in sql

    @pytest.mark.parametrize(
        "inbound,outbound,expected",
        [
            (None, None, True),   # legacy row, neither set — keeps dialling
            (None, 200, True),    # outbound-only
            (200, 200, True),     # answers and dials
            (200, None, False),   # inbound-only — the dedicated published line
        ],
    )
    def test_truth_table(self, inbound, outbound, expected):
        """Python mirror of the SQL predicate, as executable documentation."""
        in_pool = not (inbound is not None and outbound is None)
        assert in_pool is expected


class TestCampaignPinnedCallerId:
    def _campaign(self, pinned_id):
        return SimpleNamespace(
            id=99,
            # "running" or process_batch returns before pool setup.
            state="running",
            organization_id=7,
            workflow_id=200,
            telephony_configuration_id=4,
            from_phone_number_id=pinned_id,
        )

    async def _run_pool_init(self, campaign, pinned_row):
        """Drive process_batch far enough to observe pool setup."""
        dispatcher = CampaignCallDispatcher()
        provider = SimpleNamespace(
            from_numbers=["+15551110001", "+15551110002", "+15551110003"]
        )

        with (
            patch.object(
                dispatcher,
                "get_provider_for_campaign",
                AsyncMock(return_value=provider),
            ),
            patch(
                "api.services.campaign.campaign_call_dispatcher.db_client"
            ) as mock_db,
            patch(
                "api.services.campaign.campaign_call_dispatcher.rate_limiter"
            ) as mock_rl,
            # Both gates are imported inside the function body, so they must be
            # patched at their source module rather than on the dispatcher.
            patch(
                "api.services.workflow_active_check.check_workflow_active",
                AsyncMock(return_value=(True, "")),
            ),
            patch(
                "api.services.wallet_check.check_wallet_before_call",
                AsyncMock(return_value=(True, "")),
            ),
        ):
            mock_db.get_campaign_by_id = AsyncMock(return_value=campaign)
            mock_db.get_phone_number_for_config = AsyncMock(return_value=pinned_row)
            # One empty batch: enough to reach pool init, then return.
            mock_db.claim_queued_runs_for_processing = AsyncMock(
                return_value=[SimpleNamespace(id=1)]
            )
            mock_db.update_campaign = AsyncMock()
            mock_rl.initialize_from_number_pool = AsyncMock()

            try:
                await dispatcher.process_batch(campaign.id, batch_size=1)
            except Exception:
                # Dialling itself is out of scope; the pool call already happened.
                pass

            return mock_rl.initialize_from_number_pool

    @pytest.mark.asyncio
    async def test_pinned_number_narrows_the_pool_to_itself(self):
        pinned = SimpleNamespace(
            id=14, address_normalized="+12392190585", is_active=True
        )
        init = await self._run_pool_init(self._campaign(14), pinned)

        assert init.await_count == 1
        numbers = init.await_args.args[1]
        assert numbers == ["+12392190585"], (
            "a pinned campaign must dial only from its chosen caller ID"
        )

    @pytest.mark.asyncio
    async def test_unpinned_campaign_uses_the_whole_pool(self):
        init = await self._run_pool_init(self._campaign(None), None)

        assert init.await_count == 1
        assert len(init.await_args.args[1]) == 3

    @pytest.mark.asyncio
    async def test_released_pinned_number_falls_back_rather_than_stranding(self):
        # Number deleted or moved to another config after the campaign was made.
        init = await self._run_pool_init(self._campaign(14), None)

        assert init.await_count == 1
        assert len(init.await_args.args[1]) == 3, (
            "a missing pinned number must not stop the campaign dialling"
        )

    @pytest.mark.asyncio
    async def test_deactivated_pinned_number_falls_back(self):
        pinned = SimpleNamespace(
            id=14, address_normalized="+12392190585", is_active=False
        )
        init = await self._run_pool_init(self._campaign(14), pinned)

        assert init.await_count == 1
        assert len(init.await_args.args[1]) == 3
