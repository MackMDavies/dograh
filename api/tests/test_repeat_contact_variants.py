"""Unit tests for resolve_repeat_contact_text — the deterministic bucket ->
variant lookup behind repeat-contact opening variants. See
docs/superpowers/specs/2026-08-11-repeat-contact-opening-variants-design.md.
"""

from api.services.workflow.repeat_contact_variants import (
    REPEAT_CONTACT_BUCKETS,
    resolve_repeat_contact_text,
)


def test_bucket_constants_match_prior_contact_relationship_type_vocabulary():
    # Must stay in exact lockstep with memory_pre_call.py's
    # prior_contact_relationship_type values (minus "none", which
    # intentionally has no variant slot).
    assert REPEAT_CONTACT_BUCKETS == (
        "spoke_directly",
        "gatekeeper_screened",
        "no_answer",
    )


def test_returns_variant_when_bucket_matches_and_variant_present():
    result = resolve_repeat_contact_text(
        default_text="default greeting",
        bucket="gatekeeper_screened",
        variants_by_bucket={
            "spoke_directly": None,
            "gatekeeper_screened": "variant for gatekeeper",
            "no_answer": None,
        },
    )
    assert result == "variant for gatekeeper"


def test_falls_back_to_default_when_bucket_has_no_variant():
    result = resolve_repeat_contact_text(
        default_text="default greeting",
        bucket="no_answer",
        variants_by_bucket={
            "spoke_directly": "variant a",
            "gatekeeper_screened": "variant b",
            "no_answer": None,
        },
    )
    assert result == "default greeting"


def test_falls_back_to_default_when_bucket_has_empty_string_variant():
    # An author who clears a field back to "" must fall back, not speak
    # an empty string.
    result = resolve_repeat_contact_text(
        default_text="default greeting",
        bucket="spoke_directly",
        variants_by_bucket={"spoke_directly": ""},
    )
    assert result == "default greeting"


def test_none_bucket_always_falls_back_to_default():
    # "none" (first-ever contact) has no variant slot by design.
    result = resolve_repeat_contact_text(
        default_text="default greeting",
        bucket="none",
        variants_by_bucket={
            "spoke_directly": "variant a",
            "gatekeeper_screened": "variant b",
            "no_answer": "variant c",
        },
    )
    assert result == "default greeting"


def test_missing_bucket_falls_back_to_default():
    result = resolve_repeat_contact_text(
        default_text="default greeting",
        bucket=None,
        variants_by_bucket={"spoke_directly": "variant a"},
    )
    assert result == "default greeting"


def test_unrecognized_bucket_string_falls_back_to_default():
    # Defensive: an unexpected value from call_context_vars must never crash
    # or silently speak the wrong thing.
    result = resolve_repeat_contact_text(
        default_text="default greeting",
        bucket="some_future_bucket_value",
        variants_by_bucket={"spoke_directly": "variant a"},
    )
    assert result == "default greeting"


def test_empty_variants_map_falls_back_to_default():
    result = resolve_repeat_contact_text(
        default_text="default greeting",
        bucket="spoke_directly",
        variants_by_bucket={},
    )
    assert result == "default greeting"


def test_none_variants_map_falls_back_to_default():
    result = resolve_repeat_contact_text(
        default_text="default greeting",
        bucket="spoke_directly",
        variants_by_bucket=None,
    )
    assert result == "default greeting"


def test_none_default_text_with_no_variant_returns_none():
    # The greeting field is allowed to be None (no greeting configured) —
    # resolution must not fabricate text.
    result = resolve_repeat_contact_text(
        default_text=None,
        bucket="no_answer",
        variants_by_bucket={},
    )
    assert result is None
