"""Reading the knowledge Sam may use, for the sysevo_knowledge tool. The rules live in
sam_knowledge_rules.py; this is the I/O.

The documents change when a super admin edits them, not per call, so they are read once and
kept for ten minutes. A failed read returns the last good copy, or nothing: Sam then says the
team will cover it on the meeting, which is what he did before this tool existed.
"""

from __future__ import annotations

import time

import httpx
from loguru import logger

from api.constants import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
from api.services.telephony.dialer.sam_knowledge_rules import (
    DOC_KINDS,
    FAQ_KINDS,
    Entry,
    parse_doc_sections,
    parse_faq,
    prospect_safe,
)

_CACHE_SECONDS = 600
_cache: tuple[float, list[Entry]] | None = None


def entries_from_rows(rows: list[dict]) -> list[Entry]:
    entries: list[Entry] = []
    for row in rows:
        kind = str(row.get("kind") or "")
        md = str(row.get("content_md") or "")
        # 'managers' documents are internal; only what every staff member may read goes further.
        if row.get("audience") != "staff" or not md.strip():
            continue
        if kind in FAQ_KINDS:
            entries += parse_faq(kind, md)
        elif kind in DOC_KINDS:
            entries += parse_doc_sections(kind, str(row.get("title") or kind), md)
    return prospect_safe(entries)


async def load_entries() -> list[Entry]:
    global _cache
    if _cache and time.monotonic() - _cache[0] < _CACHE_SECONDS:
        return _cache[1]
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return _cache[1] if _cache else []
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/sysevo_knowledge_docs",
                params={
                    "select": "kind,title,content_md,audience",
                    "kind": f"in.({','.join(FAQ_KINDS + DOC_KINDS)})",
                },
                headers={
                    "apikey": SUPABASE_SERVICE_ROLE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                },
                timeout=4.0,
            )
            response.raise_for_status()
            rows = response.json()
        entries = entries_from_rows(rows if isinstance(rows, list) else [])
        _cache = (time.monotonic(), entries)
        return entries
    except Exception as exc:  # noqa: BLE001 - a live caller is waiting on the answer
        logger.error(f"sam-knowledge: reading sysevo_knowledge_docs failed: {exc}")
        return _cache[1] if _cache else []
