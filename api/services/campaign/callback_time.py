"""Resolve a prospect's timezone from their phone number.

A callback time is only meaningful in the PROSPECT's timezone. Sam calls US and
Canadian numbers from a UK business, so treating "2pm" as UK time would ring a
Californian at 6am. When the timezone cannot be resolved this returns None, and
the caller must route the request to review rather than dialling on a guess.
"""

from typing import Optional

# NANP area code -> IANA timezone. Not exhaustive by design: an unknown code
# must degrade to review, which is safer than assuming a coast.
_NANP_AREA_TZ: dict[str, str] = {
    # Eastern
    "212": "America/New_York", "646": "America/New_York", "917": "America/New_York",
    "718": "America/New_York", "203": "America/New_York", "215": "America/New_York",
    "404": "America/New_York", "305": "America/New_York", "416": "America/Toronto",
    # Central
    "312": "America/Chicago", "402": "America/Chicago", "512": "America/Chicago",
    "214": "America/Chicago", "713": "America/Chicago", "615": "America/Chicago",
    # Mountain
    "303": "America/Denver", "602": "America/Phoenix", "403": "America/Edmonton",
    # Pacific
    "310": "America/Los_Angeles", "415": "America/Los_Angeles",
    "206": "America/Los_Angeles", "604": "America/Vancouver",
    "619": "America/Los_Angeles", "702": "America/Los_Angeles",
}


def resolve_timezone(phone_number: Optional[str]) -> Optional[str]:
    """Best-effort IANA timezone for an E.164 number, or None when unknown."""
    if not isinstance(phone_number, str):
        return None
    digits = "".join(ch for ch in phone_number if ch.isdigit())
    if not digits:
        return None

    # UK
    if digits.startswith("44"):
        return "Europe/London"

    # NANP: country code 1 + 3-digit area code
    if digits.startswith("1") and len(digits) >= 11:
        return _NANP_AREA_TZ.get(digits[1:4])

    return None
