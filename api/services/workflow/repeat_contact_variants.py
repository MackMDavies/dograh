"""Deterministic selection of a start-node opening variant based on
prior_contact_relationship_type.

See docs/superpowers/specs/2026-08-11-repeat-contact-opening-variants-design.md.
No LLM call, no I/O — pure and safe to call on every node-opening read.
"""

from typing import Optional

# Mirrors prior_contact_relationship_type's vocabulary
# (api/services/pipecat/memory_pre_call.py), minus "none" — a first-ever
# contact has no variant slot; it always uses the node's default text.
REPEAT_CONTACT_BUCKETS: tuple[str, ...] = (
    "spoke_directly",
    "gatekeeper_screened",
    "no_answer",
)


def resolve_repeat_contact_text(
    default_text: Optional[str],
    bucket: Optional[str],
    variants_by_bucket: Optional[dict[str, Optional[str]]],
) -> Optional[str]:
    """Return the author-written variant for `bucket`, or `default_text`.

    `bucket` is expected to be the call's current
    `prior_contact_relationship_type` value (or None/missing). Only a
    non-empty variant for a recognized bucket overrides `default_text` —
    "none", an unrecognized value, a missing bucket, an empty/whitespace
    variant, or no variants map at all all fall through unchanged.
    """
    if not bucket or bucket not in REPEAT_CONTACT_BUCKETS:
        return default_text

    variant = (variants_by_bucket or {}).get(bucket)
    return variant if variant else default_text
