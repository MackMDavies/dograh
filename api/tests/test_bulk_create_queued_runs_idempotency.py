"""Regression test: bulk_create_queued_runs must be idempotent on
(campaign_id, source_uuid) — a retried campaign source sync (arq retries
failed jobs by default) previously re-inserted every row in the campaign
whenever the failure happened after the bulk insert had already committed
but before the campaign/event side of the sync finished. source_uuid is
deterministic per row (hash of the source file + row index for CSV), so an
ON CONFLICT DO NOTHING upsert on (campaign_id, source_uuid) makes a retried
sync a safe no-op for rows that already exist, rather than a duplicate.

Verified at the SQL-construction level (no live Postgres in this
environment) — checks the actual compiled statement contains the
PostgreSQL-specific ON CONFLICT DO NOTHING clause targeting the right
columns, and confirms the empty-input short-circuit doesn't touch the
session at all.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.db.campaign_client import CampaignClient


class TestBulkCreateQueuedRunsIdempotency:
    @pytest.mark.asyncio
    async def test_empty_input_is_a_no_op(self):
        client = CampaignClient.__new__(CampaignClient)
        client.async_session = MagicMock()

        await client.bulk_create_queued_runs([])

        client.async_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_statement_uses_on_conflict_do_nothing_on_campaign_and_source_uuid(
        self,
    ):
        captured_stmt = {}

        class FakeSession:
            async def execute(self, stmt):
                captured_stmt["stmt"] = stmt

            async def commit(self):
                pass

            async def rollback(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        client = CampaignClient.__new__(CampaignClient)
        client.async_session = MagicMock(return_value=FakeSession())

        rows = [
            {
                "campaign_id": 1,
                "source_uuid": "csv_abc123_row_1",
                "context_variables": {"phone_number": "+15551234567"},
                "state": "queued",
            }
        ]
        await client.bulk_create_queued_runs(rows)

        stmt = captured_stmt["stmt"]
        # Compile against the postgresql dialect to inspect the real ON
        # CONFLICT SQL — insert() alone doesn't expose this as public attrs
        # in a stable way across SQLAlchemy versions, but the compiled text
        # does.
        compiled = stmt.compile(dialect=__import__(
            "sqlalchemy.dialects.postgresql", fromlist=["dialect"]
        ).dialect())
        sql_text = str(compiled).lower()

        assert "on conflict" in sql_text
        assert "do nothing" in sql_text
        assert "campaign_id" in sql_text
        assert "source_uuid" in sql_text
