"""Collapse duplicate voice-library rows for display.

Duplicates arise two ways:

1. The provider sync checks for an existing voice and then inserts, with no
   unique constraint behind it — two concurrent syncs both pass the check and
   both insert. (Production carries ~930 such rows.)
2. ``list_voices`` lets a superuser see voices across ALL organisations, so a
   provider voice synced into several orgs shows up once per org.

Both surface in the agent voice picker as the same voice listed twice. This
collapses them by the identity that actually matters — the provider and the
provider-assigned voice id — keeping the lowest row id so the selection a
workflow already stores stays valid.

Rows without a provider voice id (a clone still being processed) are never
collapsed: they have no shared identity yet, and folding them together would
hide every pending clone but one.
"""

from typing import Iterable, TypeVar

T = TypeVar("T")


def dedupe_voices(voices: Iterable[T]) -> list[T]:
    """Return *voices* with duplicate provider voices removed.

    For each duplicate group the LOWEST row id wins — that is the row that
    existed before the double-insert, so any workflow already pointing at it
    keeps resolving. Output follows the order each voice first appeared in the
    input, so caller-side sorting is preserved.
    """
    voices = list(voices)
    best: dict[tuple[str, str], T] = {}
    order: list[tuple[str, str]] = []
    out: list[T] = []

    for voice in voices:
        provider_voice_id = getattr(voice, "provider_voice_id", None)
        if not provider_voice_id:
            # No stable identity yet (e.g. a clone mid-processing) — always keep.
            out.append(voice)
            continue

        key = (getattr(voice, "provider", "") or "", provider_voice_id)
        current = best.get(key)
        if current is None:
            best[key] = voice
            order.append(key)
            out.append(voice)
            continue

        # Same voice seen again — keep whichever row is older (lower id).
        if _row_id(voice) < _row_id(current):
            best[key] = voice
            out[out.index(current)] = voice

    return out


def _row_id(voice) -> float:
    """Row id for age comparison; rows without one sort last."""
    value = getattr(voice, "id", None)
    return value if isinstance(value, int) else float("inf")
