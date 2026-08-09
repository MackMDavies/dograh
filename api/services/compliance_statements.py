"""Canonical wording for compliance acknowledgements, and its version.

The evidentiary question in a dispute is not "did they tick a box" but "what
exactly did they agree to". That needs the wording pinned somewhere the server
controls, versioned so a later edit cannot silently rewrite history.

The version is a hash of the text, so it changes automatically when the wording
changes — a human-maintained version number would eventually be forgotten on an
edit, and a stale version is worse than none because it looks authoritative.

The UI renders its own copy of this text. Both are recorded on each
acknowledgement (server canonical + client-reported) precisely because they can
drift; a mismatch is a fact worth having rather than one to assume away.
"""

import hashlib

ACK_CALLING_HOURS_OFF = "calling_hours_off"

CALLING_HOURS_OFF_STATEMENT = (
    "Calling outside standard hours may expose you to legal liability under "
    "telemarketing law. This removes all calling-hours enforcement for this "
    "campaign, including the legal floor."
)


def statement_version(text: str) -> str:
    """Short, stable fingerprint of a statement's exact wording."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


CALLING_HOURS_OFF_VERSION = statement_version(CALLING_HOURS_OFF_STATEMENT)
