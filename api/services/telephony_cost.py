"""Attach the CARRIER charge to a completed call.

`cost_info.cost_breakdown` covers what the pipeline burned — LLM, speech
synthesis, transcription. It does NOT include what Twilio charged to carry the
call, and on real telephony that is roughly a THIRD of the true cost: measured
across 71 priced calls, Dograh $3.36 vs Twilio $1.88, i.e. every cost figure in
the product was understating reality by ~36%.

Two details make this easy to get wrong:

* Twilio reports `price` as a NEGATIVE number (its charge convention). Stored
  here as a positive amount.
* Twilio bills in the ACCOUNT's currency — GBP on this account — while every
  Dograh cost is USD. Summing them raw is wrong by the FX rate. The currency is
  recorded alongside the amount and converted downstream against ECB rates;
  nothing here pretends a GBP figure is dollars.

Prices are populated ASYNCHRONOUSLY by Twilio — `price` is usually null for
minutes after a call ends. That is why this is a reconciler rather than
something the completion task does inline.
"""

import base64
import json
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Optional

from loguru import logger

_MATCH_WINDOW_SECONDS = 120


def _digits_tail(value: Optional[str], n: int = 10) -> str:
    d = "".join(c for c in (value or "") if c.isdigit())
    return d[-n:] if len(d) >= n else d


def _parse_twilio_time(value: str) -> Optional[float]:
    try:
        return datetime.strptime(value, "%a, %d %b %Y %H:%M:%S %z").timestamp()
    except Exception:
        return None


def fetch_priced_calls(
    account_sid: str, auth_token: str, *, page_size: int = 1000, max_calls: int = 3000
) -> list[dict[str, Any]]:
    """Return Twilio Call resources that already carry a price."""
    auth = "Basic " + base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json?"
        + urllib.parse.urlencode({"PageSize": page_size})
    )
    out: list[dict[str, Any]] = []
    while url and len(out) < max_calls:
        req = urllib.request.Request(url)
        req.add_header("Authorization", auth)
        try:
            payload = json.load(urllib.request.urlopen(req, timeout=40))
        except Exception as exc:  # noqa: BLE001 — cost enrichment must never break a call
            logger.warning(f"[telephony-cost] Twilio list failed: {exc}")
            break
        out.extend(c for c in payload.get("calls", []) if c.get("price"))
        nxt = payload.get("next_page_uri")
        url = ("https://api.twilio.com" + nxt) if nxt else None
    return out


def index_by_destination(calls: list[dict[str, Any]]) -> dict[str, list[tuple]]:
    """Group priced calls by the last 10 digits of the number dialled."""
    idx: dict[str, list[tuple]] = {}
    for c in calls:
        started = _parse_twilio_time(c.get("start_time") or "")
        if started is None:
            continue
        try:
            price = abs(float(c["price"]))  # Twilio reports charges as negative
        except (TypeError, ValueError):
            continue
        idx.setdefault(_digits_tail(c.get("to")), []).append(
            (started, c.get("sid"), price, c.get("price_unit"))
        )
    return idx


def match_call(
    idx: dict[str, list[tuple]],
    *,
    destination: Optional[str],
    started_epoch: float,
    window_seconds: int = _MATCH_WINDOW_SECONDS,
) -> Optional[dict[str, Any]]:
    """Find the carrier record for one run.

    Matched on (number dialled, start time) rather than a stored CallSid because
    no CallSid was persisted historically. The number alone is not enough — the
    same prospect can be dialled repeatedly — so the closest start time within a
    tight window decides, and nothing is returned if the gap is too wide. A wrong
    match here would attach one call's carrier charge to another.
    """
    candidates = idx.get(_digits_tail(destination), [])
    if not candidates:
        return None
    delta, sid, price, unit = min(
        ((abs(st - started_epoch), sid, pr, un) for st, sid, pr, un in candidates),
        default=(None, None, None, None),
    )
    if delta is None or delta > window_seconds:
        return None
    return {"call_sid": sid, "price": price, "currency": unit, "match_delta_s": delta}
