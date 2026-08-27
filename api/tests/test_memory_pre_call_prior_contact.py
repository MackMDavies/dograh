"""Reconciliation logic in _augment_with_prior_contact for the returning-caller
disposition fields (has_been_called_before, prior_contact_relationship_type),
which arrive from dograh-memory-inbound-hook's contact_disposition_state
lookup and must be merged with prior_contact.py's own workflow_runs signal —
two independent sources describing the same "did we call before" question.
"""

from unittest.mock import AsyncMock, patch

import pytest

from api.services.pipecat.memory_pre_call import _augment_with_prior_contact


@pytest.mark.asyncio
async def test_disposition_signal_wins_when_both_present():
    """The hook already found a real disposition — prior_contact.py agreeing
    (or disagreeing) must not overwrite the richer disposition-derived value."""
    with patch(
        "api.services.pipecat.prior_contact.lookup_prior_outbound_contact",
        new=AsyncMock(return_value={"full_name": "Mack", "prior_attempts": 2}),
    ):
        out = await _augment_with_prior_contact(
            {
                "has_been_called_before": "true",
                "prior_contact_relationship_type": "gatekeeper_screened",
            },
            {"called_number": "+15555550101"},
            organization_id=1,
            call_type="outbound",
        )
    assert out["has_been_called_before"] == "true"
    assert out["prior_contact_relationship_type"] == "gatekeeper_screened"


@pytest.mark.asyncio
async def test_prior_contact_fills_the_gap_when_hook_found_no_disposition():
    """No disposition row at all (predates the disposition system, or
    classification silently failed) but prior_contact.py DID find a prior
    dial — that still counts as called-before, mapped to no_answer since
    there's no real classification behind it."""
    with patch(
        "api.services.pipecat.prior_contact.lookup_prior_outbound_contact",
        new=AsyncMock(return_value={"full_name": "Mack", "prior_attempts": 1}),
    ):
        out = await _augment_with_prior_contact(
            {},  # hook found nothing
            {"called_number": "+15555550102"},
            organization_id=1,
            call_type="outbound",
        )
    assert out["has_been_called_before"] == "true"
    assert out["prior_contact_relationship_type"] == "no_answer"


@pytest.mark.asyncio
async def test_genuine_stranger_stays_cold():
    """Neither signal found anything — must not fabricate a prior contact."""
    with patch(
        "api.services.pipecat.prior_contact.lookup_prior_outbound_contact",
        new=AsyncMock(return_value={}),
    ):
        out = await _augment_with_prior_contact(
            {},
            {"called_number": "+15555550103"},
            organization_id=1,
            call_type="outbound",
        )
    assert out["has_been_called_before"] == "false"
    assert out["prior_contact_relationship_type"] == "none"
    assert out["caller_known_from"] == "none"


@pytest.mark.asyncio
async def test_missing_keys_default_to_safe_strings_not_none():
    """Every key the prompt can reference must exist as a string — a bare
    None renders as the literal word "None" in a live greeting."""
    with patch(
        "api.services.pipecat.prior_contact.lookup_prior_outbound_contact",
        new=AsyncMock(return_value={}),
    ):
        out = await _augment_with_prior_contact(
            {},
            {"called_number": "+15555550104"},
            organization_id=1,
            call_type="outbound",
        )
    for key in (
        "last_disposition_bucket",
        "last_disposition_code",
        "days_since_last_call",
        "has_been_called_before",
        "prior_contact_relationship_type",
    ):
        assert key in out
        assert out[key] is not None
        assert isinstance(out[key], str)


@pytest.mark.asyncio
async def test_real_memory_conversation_is_unaffected_by_disposition_fields():
    """caller_known=true (a real transcript exists) must still resolve to
    caller_known_from=memory regardless of what the disposition fields say —
    the two are orthogonal signals from the plan's design."""
    with patch(
        "api.services.pipecat.prior_contact.lookup_prior_outbound_contact",
        new=AsyncMock(return_value={}),
    ):
        out = await _augment_with_prior_contact(
            {
                "caller_known": "true",
                "caller_name": "Mack",
                "has_been_called_before": "true",
                "prior_contact_relationship_type": "spoke_directly",
            },
            {"called_number": "+15555550105"},
            organization_id=1,
            call_type="outbound",
        )
    assert out["caller_known_from"] == "memory"
    assert out["has_been_called_before"] == "true"
    assert out["prior_contact_relationship_type"] == "spoke_directly"


@pytest.mark.asyncio
async def test_prior_contact_lookup_failure_does_not_block_disposition_defaults():
    """If prior_contact.py's DB lookup throws, the whole call must still
    proceed with safe defaults rather than propagate the error."""
    with patch(
        "api.services.pipecat.prior_contact.lookup_prior_outbound_contact",
        new=AsyncMock(side_effect=RuntimeError("db unreachable")),
    ):
        out = await _augment_with_prior_contact(
            {"has_been_called_before": "true", "prior_contact_relationship_type": "no_answer"},
            {"called_number": "+15555550106"},
            organization_id=1,
            call_type="outbound",
        )
    # Disposition fields already present from the hook must survive a prior_contact.py failure.
    assert out["has_been_called_before"] == "true"
    assert out["prior_contact_relationship_type"] == "no_answer"
