"""reseed_models_for_connection must preserve admin-set pricing on resync.

The function deletes and recreates every org_available_models row for a
connection on every "Re-sync Models" action, and already preserves
is_client_available, is_default, cost_per_min_usd, and native_cost_display
from the row being replaced. our_price_per_min_usd — the admin's manually
set (or margin-applied) per-minute price, the sole data source for the
agent-page cost calculator and the admin-only price-per-model dropdown list —
was never carried forward, so it silently reset to null on every resync.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.db.provider_connection_client import ProviderConnectionClient


def _make_connection(conn_id=1, provider="openai", service_type="llm", api_key="sk-test"):
    conn = MagicMock()
    conn.id = conn_id
    conn.provider = provider
    conn.service_type = service_type
    conn.api_key = api_key
    conn.is_active = True
    return conn


def _make_existing_model(
    model_id="gpt-4.1",
    our_price=0.05,
    cost_per_min=0.02,
    native_display="$0.02/min",
    is_client_available=False,
    is_default=True,
):
    m = MagicMock()
    m.model_id = model_id
    m.is_client_available = is_client_available
    m.is_default = is_default
    m.cost_per_min_usd = cost_per_min
    m.native_cost_display = native_display
    m.our_price_per_min_usd = our_price
    return m


def _make_two_stage_sessions(conn, existing_models):
    """Build the two async-session mocks reseed_models_for_connection opens in
    sequence: one for reading the connection + existing rows, one for the
    delete + re-insert."""
    conn_result = MagicMock()
    conn_result.scalar_one_or_none.return_value = conn

    existing_result = MagicMock()
    existing_result.scalars.return_value.all.return_value = existing_models

    first_session = AsyncMock()
    first_session.execute = AsyncMock(side_effect=[conn_result, existing_result])
    first_session.__aenter__ = AsyncMock(return_value=first_session)
    first_session.__aexit__ = AsyncMock(return_value=None)

    second_session = AsyncMock()
    second_session.execute = AsyncMock(return_value=MagicMock())  # the DELETE
    second_session.__aenter__ = AsyncMock(return_value=second_session)
    second_session.__aexit__ = AsyncMock(return_value=None)
    added = []
    second_session.add = MagicMock(side_effect=lambda obj: added.append(obj))

    return first_session, second_session, added


@pytest.mark.asyncio
async def test_reseed_preserves_admin_set_our_price_when_catalog_has_no_pricing():
    """Live API returns the same model, catalog pricing lookup misses (common for
    a model an admin priced manually with no public catalog entry) — the prior
    our_price_per_min_usd must still survive the resync."""
    client = ProviderConnectionClient()
    conn = _make_connection()
    existing_model = _make_existing_model(model_id="gpt-4.1", our_price=0.05)

    first_session, second_session, added = _make_two_stage_sessions(conn, [existing_model])

    with patch.object(client, "async_session") as mock_async_session, \
         patch("api.db.provider_connection_client._fetch_live_models", AsyncMock(return_value=["gpt-4.1"])), \
         patch("api.db.provider_connection_client.get_model_pricing", return_value=None):
        mock_async_session.side_effect = [first_session, second_session]
        count = await client.reseed_models_for_connection(connection_id=1, organization_id=11)

    assert count == 1
    assert len(added) == 1
    assert added[0].our_price_per_min_usd == 0.05, (
        "our_price_per_min_usd must be carried forward from the prior row, "
        "the same way is_client_available/is_default/cost_per_min_usd already are"
    )


@pytest.mark.asyncio
async def test_reseed_preserves_admin_set_our_price_when_catalog_has_pricing():
    """Even when the static catalog DOES have a cost_per_min_usd/native_cost_display
    for this model (so those two fields get freshly repopulated rather than
    carried from prev), our_price_per_min_usd — the admin's own markup, which the
    catalog has no opinion on — must still be preserved."""
    client = ProviderConnectionClient()
    conn = _make_connection()
    existing_model = _make_existing_model(model_id="gpt-4.1", our_price=0.08, cost_per_min=0.02)

    first_session, second_session, added = _make_two_stage_sessions(conn, [existing_model])

    with patch.object(client, "async_session") as mock_async_session, \
         patch("api.db.provider_connection_client._fetch_live_models", AsyncMock(return_value=["gpt-4.1"])), \
         patch("api.db.provider_connection_client.get_model_pricing", return_value=(0.03, "$0.03/min")):
        mock_async_session.side_effect = [first_session, second_session]
        await client.reseed_models_for_connection(connection_id=1, organization_id=11)

    assert added[0].cost_per_min_usd == 0.03  # freshly repopulated from the catalog
    assert added[0].our_price_per_min_usd == 0.08  # the admin's markup, untouched


@pytest.mark.asyncio
async def test_reseed_leaves_our_price_null_for_a_genuinely_new_model():
    """A model with no prior row (never priced by an admin) has no price to
    preserve — it should come back null, not error or fabricate a value."""
    client = ProviderConnectionClient()
    conn = _make_connection()

    first_session, second_session, added = _make_two_stage_sessions(conn, [])  # nothing existing

    with patch.object(client, "async_session") as mock_async_session, \
         patch("api.db.provider_connection_client._fetch_live_models", AsyncMock(return_value=["gpt-4.1"])), \
         patch("api.db.provider_connection_client.get_model_pricing", return_value=None):
        mock_async_session.side_effect = [first_session, second_session]
        await client.reseed_models_for_connection(connection_id=1, organization_id=11)

    assert added[0].our_price_per_min_usd is None
