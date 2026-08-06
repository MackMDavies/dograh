"""Fallback-syntax behaviour for {{var | default}} template placeholders.

These guard the opener/first-message path, where an unresolved variable would
otherwise render as an empty string and the agent would say "Hi ...?".
"""

from api.utils.template_renderer import render_template


def test_fallback_used_when_variable_missing():
    assert render_template("Hi {{first_name | there}}!", {}) == "Hi there!"


def test_fallback_used_when_variable_is_empty_string():
    assert render_template("Hi {{first_name | there}}!", {"first_name": ""}) == "Hi there!"


def test_real_value_wins_over_fallback():
    assert render_template("Hi {{first_name | there}}!", {"first_name": "Mack"}) == "Hi Mack!"


def test_fallback_containing_a_colon_is_not_truncated():
    """Legacy syntax is {{var | fallback:default}}; a plain default that merely
    contains a colon must survive intact rather than being cut at the colon."""
    assert (
        render_template("{{greeting | Hi: friend}}", {}) == "Hi: friend"
    )


def test_legacy_fallback_filter_syntax_still_supported():
    assert render_template("{{name | fallback:Unknown}}", {}) == "Unknown"


def test_multi_word_fallback_preserved():
    assert (
        render_template("is this {{company | your business}}?", {})
        == "is this your business?"
    )


def test_descriptor_object_never_rendered_as_json():
    """If a canonical variable descriptor leaks into the context, the agent must
    not speak raw JSON. It should render as empty and let the fallback apply."""
    context = {"email": {"default": "", "source": "memory", "memory_attr": "email"}}
    assert render_template("{{email | no email}}", context) == "no email"
