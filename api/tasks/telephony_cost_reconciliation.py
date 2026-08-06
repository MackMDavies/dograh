"""Attach Twilio's carrier charge to completed calls (safety-net cron).

`cost_info.cost_breakdown` is the PIPELINE cost — LLM, TTS, STT. It has never
included what the carrier charged to move the audio, and on real telephony that
is about a third of the true cost (measured: Dograh $3.36 vs Twilio $1.88 across
71 priced calls). Any "what did this call cost" figure without it is wrong by
roughly 36%.

This runs as a cron rather than inline at call completion because Twilio prices
asynchronously: `price` is null for minutes after a call ends, so asking at
hang-up almost always returns nothing.

Writes `cost_info.telephony` = {amount, currency, call_sid, source}. The amount
is left in the CARRIER'S currency (GBP on this account) and explicitly labelled;
converting to USD is the consumer's job, against real FX rates. Silently
treating a GBP figure as dollars is exactly the error this shape prevents.

Mirrors api/tasks/memory_reconciliation.py.
"""

import json
from typing import Any

from loguru import logger
from sqlalchemy import text

from api.db import db_client
from api.services.telephony_cost import (
    fetch_priced_calls,
    index_by_destination,
    match_call,
)

# Only look back far enough to catch anything the previous runs missed; Twilio
# prices settle within hours, so a wide window is wasted API calls.
_LOOKBACK = "14 days"
_MAX_PER_RUN = 300


async def reconcile_telephony_cost(_ctx) -> None:
    """Fill in the carrier charge for recent telephony runs that lack one."""
    sql = text(
        """
        SELECT r.id,
               extract(epoch FROM r.created_at) AS started,
               COALESCE(
                   r.initial_context::jsonb ->> 'called_number',
                   r.initial_context::jsonb ->> 'phone_number'
               ) AS destination,
               r.initial_context::jsonb ->> 'telephony_configuration_id' AS cfg_id
        FROM workflow_runs r
        WHERE r.created_at > now() - interval '{lookback}'
          AND r.initial_context::jsonb ? 'called_number'
          AND NOT COALESCE(r.cost_info::jsonb ? 'telephony', false)
        ORDER BY r.created_at DESC
        LIMIT {limit}
        """.format(lookback=_LOOKBACK, limit=_MAX_PER_RUN)
    )

    try:
        async with db_client.async_session() as session:
            rows = (await session.execute(sql)).mappings().all()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[telephony-cost] could not list runs: {exc}")
        return

    if not rows:
        return

    # One Twilio listing serves every run — never one request per call.
    by_cfg: dict[int, dict] = {}
    filled = 0

    for row in rows:
        try:
            cfg_id = int(row["cfg_id"]) if row["cfg_id"] else None
        except (TypeError, ValueError):
            cfg_id = None
        if cfg_id is None:
            continue

        if cfg_id not in by_cfg:
            try:
                from api.db.telephony_phone_number_client import _decrypted_credentials

                cfg = await db_client.get_telephony_configuration(cfg_id)
                creds = _decrypted_credentials(cfg) if cfg else {}
                sid, tok = creds.get("account_sid"), creds.get("auth_token")
                by_cfg[cfg_id] = (
                    index_by_destination(fetch_priced_calls(sid, tok))
                    if sid and tok
                    else {}
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[telephony-cost] config {cfg_id} unusable: {exc}")
                by_cfg[cfg_id] = {}

        idx = by_cfg.get(cfg_id) or {}
        if not idx:
            continue

        hit = match_call(
            idx, destination=row["destination"], started_epoch=float(row["started"])
        )
        if not hit:
            continue

        payload: dict[str, Any] = {
            "amount": hit["price"],
            # NOT dollars. Twilio bills in the account's currency; the consumer
            # converts. See the module docstring.
            "currency": hit["currency"],
            "call_sid": hit["call_sid"],
            "source": "twilio_api",
        }
        try:
            async with db_client.async_session() as session:
                await session.execute(
                    text(
                        # cast(:p as jsonb), NOT :p::jsonb — SQLAlchemy's text()
                        # binder mis-reads a `::` cast beside a bind parameter and
                        # silently drops the parameter.
                        "UPDATE workflow_runs "
                        "SET cost_info = (COALESCE(cast(cost_info AS jsonb), '{}'::jsonb) "
                        "                 || jsonb_build_object('telephony', cast(:p AS jsonb)))::json "
                        "WHERE id = :id"
                    ),
                    {"p": json.dumps(payload), "id": row["id"]},
                )
                await session.commit()
            filled += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[telephony-cost] run {row['id']} update failed: {exc}")

    if filled:
        logger.info(
            f"[telephony-cost] attached carrier charges to {filled} of {len(rows)} runs"
        )
