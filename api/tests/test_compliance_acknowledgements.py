"""The audit record for waiving calling-hours enforcement.

Enforcement already existed — the wizard blocks Continue and the request model
refuses mode='off' without a timestamp. What did not exist was evidence: the
timestamp lived in a mutable JSON blob with no actor, no wording, and no
history, so an update silently overwrote the previous acknowledgement.

These tests pin the properties that make the new table usable as evidence
rather than as a flag.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.routes.campaign import (
    CallingHoursConfigRequest,
    _record_calling_hours_acknowledgement,
)
from api.services.compliance_statements import (
    ACK_CALLING_HOURS_OFF,
    CALLING_HOURS_OFF_STATEMENT,
    CALLING_HOURS_OFF_VERSION,
    statement_version,
)


def _campaign(**kw):
    return SimpleNamespace(
        id=kw.get("id", 41),
        organization_id=kw.get("organization_id", 3),
        name=kw.get("name", "Q3 Outreach"),
        state=kw.get("state", "created"),
    )


def _user(user_id=7):
    return SimpleNamespace(id=user_id, is_superuser=False, selected_organization_id=3)


def _http_request(ip="203.0.113.9", ua="Mozilla/5.0", forwarded=None):
    headers = {"user-agent": ua}
    if forwarded is not None:
        headers["x-forwarded-for"] = forwarded
    return SimpleNamespace(
        headers=headers, client=SimpleNamespace(host=ip)
    )


class TestEnforcementStillHolds:
    """The guard that predates this work must not have been loosened."""

    def test_off_without_acknowledgement_cannot_be_constructed(self):
        with pytest.raises(ValueError, match="off_acknowledged_at"):
            CallingHoursConfigRequest(mode="off")

    def test_off_with_acknowledgement_is_valid(self):
        req = CallingHoursConfigRequest(
            mode="off", off_acknowledged_at="2026-08-09T05:00:00Z"
        )
        assert req.mode == "off"


class TestWhatGetsRecorded:
    @pytest.mark.asyncio
    async def test_records_actor_campaign_and_statement(self):
        with patch("api.routes.campaign.db_client") as mock_db:
            mock_db.record_acknowledgement = AsyncMock()
            await _record_calling_hours_acknowledgement(
                CallingHoursConfigRequest(
                    mode="off",
                    off_acknowledged_at="2026-08-09T05:00:00Z",
                    off_acknowledged_statement="what the UI showed",
                ),
                _campaign(),
                _user(user_id=7),
                _http_request(),
            )

            kw = mock_db.record_acknowledgement.await_args.kwargs
            assert kw["user_id"] == 7, "an acknowledgement with no actor is not evidence"
            assert kw["organization_id"] == 3
            assert kw["campaign_id"] == 41
            assert kw["campaign_name"] == "Q3 Outreach", (
                "denormalised so the record still names its subject after the "
                "campaign is deleted"
            )
            assert kw["acknowledgement_type"] == ACK_CALLING_HOURS_OFF
            assert kw["statement_text"] == CALLING_HOURS_OFF_STATEMENT
            assert kw["statement_version"] == CALLING_HOURS_OFF_VERSION
            assert kw["client_statement_text"] == "what the UI showed"

    @pytest.mark.asyncio
    async def test_uses_the_users_timestamp_not_now(self):
        """The moment of acceptance, not the moment of the write."""
        with patch("api.routes.campaign.db_client") as mock_db:
            mock_db.record_acknowledgement = AsyncMock()
            await _record_calling_hours_acknowledgement(
                CallingHoursConfigRequest(
                    mode="off", off_acknowledged_at="2026-08-09T05:00:00Z"
                ),
                _campaign(),
                _user(),
                None,
            )
            got = mock_db.record_acknowledgement.await_args.kwargs["acknowledged_at"]
            assert got == datetime(2026, 8, 9, 5, 0, tzinfo=timezone.utc)

    @pytest.mark.asyncio
    async def test_prefers_forwarded_ip_over_the_proxy(self):
        """The app sits behind nginx and a tunnel, so client.host is the proxy."""
        with patch("api.routes.campaign.db_client") as mock_db:
            mock_db.record_acknowledgement = AsyncMock()
            await _record_calling_hours_acknowledgement(
                CallingHoursConfigRequest(
                    mode="off", off_acknowledged_at="2026-08-09T05:00:00Z"
                ),
                _campaign(),
                _user(),
                _http_request(ip="172.18.0.1", forwarded="198.51.100.4, 172.18.0.1"),
            )
            ctx = mock_db.record_acknowledgement.await_args.kwargs["context"]
            assert ctx["ip"] == "198.51.100.4"
            assert ctx["user_agent"] == "Mozilla/5.0"


class TestWhatIsNotRecorded:
    @pytest.mark.parametrize("mode", ["inherit", "custom"])
    @pytest.mark.asyncio
    async def test_compliant_modes_write_nothing(self, mode):
        """Only a waiver is a risk acceptance. inherit/custom stay inside the
        legal floor, so there is nothing to evidence and a row would be noise."""
        cfg = (
            CallingHoursConfigRequest(mode="custom", start="09:00", end="18:00")
            if mode == "custom"
            else CallingHoursConfigRequest(mode="inherit")
        )
        with patch("api.routes.campaign.db_client") as mock_db:
            mock_db.record_acknowledgement = AsyncMock()
            await _record_calling_hours_acknowledgement(cfg, _campaign(), _user(), None)
            assert mock_db.record_acknowledgement.await_count == 0

    @pytest.mark.asyncio
    async def test_no_calling_hours_block_writes_nothing(self):
        with patch("api.routes.campaign.db_client") as mock_db:
            mock_db.record_acknowledgement = AsyncMock()
            await _record_calling_hours_acknowledgement(None, _campaign(), _user(), None)
            assert mock_db.record_acknowledgement.await_count == 0


class TestFailureIsolation:
    @pytest.mark.asyncio
    async def test_a_failed_audit_write_does_not_break_the_campaign(self):
        """Enforcement already happened upstream. Losing the user's campaign
        because the evidence write failed trades a real outcome for a
        bookkeeping one — the client logs loudly instead."""
        with patch("api.routes.campaign.db_client") as mock_db:
            mock_db.record_acknowledgement = AsyncMock(
                side_effect=RuntimeError("db down")
            )
            with pytest.raises(RuntimeError):
                # The client swallows this in production; here we assert the
                # helper itself does not add a second layer that would hide a
                # bug in the client's own error handling.
                await _record_calling_hours_acknowledgement(
                    CallingHoursConfigRequest(
                        mode="off", off_acknowledged_at="2026-08-09T05:00:00Z"
                    ),
                    _campaign(),
                    _user(),
                    None,
                )


class TestStatementVersioning:
    def test_version_is_derived_from_the_text(self):
        assert CALLING_HOURS_OFF_VERSION == statement_version(
            CALLING_HOURS_OFF_STATEMENT
        )

    def test_changing_the_wording_changes_the_version(self):
        """A human-maintained version number would eventually be forgotten on
        an edit, and a stale version is worse than none — it looks authoritative
        while describing wording nobody ever saw."""
        assert statement_version("a") != statement_version("b")

    def test_current_wording_is_pinned(self):
        """Fails deliberately if the statement is edited. Changing it is fine —
        update this hash in the same commit, so the change is visible in review
        rather than silently rewriting what past users are recorded as
        accepting."""
        assert CALLING_HOURS_OFF_VERSION == "8f08beaa6aa0394c"
