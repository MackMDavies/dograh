"""Recognise an inbound caller we have dialled before.

Someone ringing this number is usually ringing *back*: we called them from an
outbound campaign, they missed it or asked us to try later, and now they are
returning the call. We already know exactly who they are — their name, company
and email came from the campaign list and are sitting on the original run's
``initial_context``. Answering a returning prospect with "Hi, is that Mack from
TesterAI? Thanks for calling back" is a categorically different conversation
from "Hello, who's calling?".

This is deliberately separate from Sysevo caller MEMORY, which covers the other
case: someone we have actually *spoken* to. Memory is richer and wins when both
exist; this fills the gap when we dialled but never connected — which is the
majority of a cold-calling operation.

Lives on the Dograh side because ``workflow_runs`` is Dograh's table; the Sysevo
memory hook is an edge function and cannot see it.
"""

from typing import Any, Optional

from loguru import logger
from sqlalchemy import text

from api.db import db_client


def _digits(value: Optional[str]) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def _match_key(value: Optional[str]) -> str:
    """Last 10 digits — the comparison the rest of the stack already uses.

    Numbers reach us in mixed shapes (+1 404…, 1404…, 404…) depending on whether
    they came from a CSV, Twilio or a human. Comparing whole strings misses the
    same person written differently, which in this context means failing to
    recognise a returning prospect.
    """
    d = _digits(value)
    return d[-10:] if len(d) >= 10 else d


async def lookup_prior_outbound_contact(
    phone_number: Optional[str],
    *,
    organization_id: Optional[int] = None,
    exclude_run_id: Optional[int] = None,
) -> dict[str, Any]:
    """Return what we know about this number from previous outbound attempts.

    Returns {} when we have never dialled them, or on any error — recognition is
    an enhancement and must never stop a call from being answered.

    `exclude_run_id` must be the run being set up. On an OUTBOUND call the
    current run is already in workflow_runs carrying this very number, so
    without it the call finds ITSELF: prior_attempts is inflated by one and a
    first-ever dial looks like a callback.

    Keys: first_name, last_name, full_name, company, email, prior_attempts,
    last_attempt_at.
    """
    key = _match_key(phone_number)
    if len(key) < 7:
        return {}

    # '[^0-9]' rather than the usual '\\D': a backslash in the SQL text was
    # rejected by Postgres ('syntax error at or near "\\"'), and the character
    # class is exactly equivalent without the escaping hazard.
    # Match on the number we DIALLED. `caller_number` on an outbound run is our
    # own caller ID and is identical on every row, so matching it would return
    # an arbitrary stranger for every inbound call.
    sql_template = """
        SELECT
            r.initial_context::jsonb ->> 'first_name'  AS first_name,
            r.initial_context::jsonb ->> 'last_name'   AS last_name,
            r.initial_context::jsonb ->> 'full_name'   AS full_name,
            r.initial_context::jsonb ->> 'name'        AS name,
            r.initial_context::jsonb ->> 'company'     AS company,
            r.initial_context::jsonb ->> 'email'       AS email,
            r.created_at,
            COUNT(*) OVER () AS prior_attempts
        FROM workflow_runs r
        WHERE r.call_type = 'outbound'
          AND r.initial_context IS NOT NULL
          AND right(regexp_replace(
                COALESCE(
                    r.initial_context::jsonb ->> 'called_number',
                    r.initial_context::jsonb ->> 'phone_number'
                ), '[^0-9]', '', 'g'), 10) = :key
        {org_clause}
        {exclude_clause}
        ORDER BY r.created_at DESC
        LIMIT 1
        """

    # Built conditionally rather than with `:org_id::int IS NULL OR ...`:
    # SQLAlchemy's text() binder mis-reads the `::int` cast next to a bind param
    # and silently drops the parameter.
    params: dict[str, Any] = {"key": key}
    # Built conditionally for the same reason as org_clause below.
    exclude_clause = ""
    if exclude_run_id is not None:
        exclude_clause = "AND r.id <> :exclude_run_id"
        params["exclude_run_id"] = exclude_run_id
    org_clause = ""
    if organization_id is not None:
        # organization_id lives on `workflows`, NOT `workflow_runs` — filtering
        # it directly raised UndefinedColumnError and silently disabled
        # recognition for every caller.
        org_clause = (
            "AND EXISTS (SELECT 1 FROM workflows w "
            "WHERE w.id = r.workflow_id AND w.organization_id = :org_id)"
        )
        params["org_id"] = organization_id
    sql = text(
        sql_template.format(org_clause=org_clause, exclude_clause=exclude_clause)
    )

    try:
        async with db_client.async_session() as session:
            row = (await session.execute(sql, params)).mappings().first()
    except Exception as exc:  # noqa: BLE001 — never block answering a call
        logger.warning(f"[prior-contact] lookup failed for ***{key[-4:]}: {exc}")
        return {}

    if not row:
        return {}

    def clean(v: Any) -> Optional[str]:
        if not isinstance(v, str):
            return None
        s = v.strip()
        # Unrendered template placeholders leak into campaign rows when a CSV
        # column is missing; one is already stored elsewhere as a phone number.
        if not s or s[0] in "[{<":
            return None
        return s

    first = clean(row.get("first_name"))
    last = clean(row.get("last_name"))
    full = clean(row.get("full_name")) or clean(row.get("name"))
    joined = " ".join(p for p in (first, last) if p).strip() or None

    result = {
        "first_name": first,
        "last_name": last,
        "full_name": full or joined,
        "company": clean(row.get("company")),
        "email": clean(row.get("email")),
        "prior_attempts": int(row.get("prior_attempts") or 0),
        "last_attempt_at": row.get("created_at"),
    }
    if not any((result["full_name"], result["company"], result["email"])):
        return {}

    logger.info(
        f"[prior-contact] inbound ***{key[-4:]} matched a previous outbound attempt: "
        f"name={result['full_name']!r} company={result['company']!r} "
        f"attempts={result['prior_attempts']}"
    )
    return result
