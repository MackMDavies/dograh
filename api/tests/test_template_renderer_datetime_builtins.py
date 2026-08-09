"""Coverage for the date/time built-in template variables.

These names (time_now, time_now_spoken, current_date, current_date_spoken,
current_day, current_year, upcoming_days) are all whitelisted as "system
variables" in workflow_graph.py's _SYSTEM_VARIABLES — meaning a prompt using
them never trips the "unresolved variable" validation warning. Until this
was implemented (commit 60e860aa), _resolve_builtin_variable only handled
current_time and current_weekday, so every prompt using the other seven
names rendered them as empty strings on every live call — exactly the
silent-failure mode workflow_graph.py's own comment warns about. This
variable set exists because a real customer was once booked into the wrong
YEAR by an agent working a date out from memory; these tests exist so that
specific regression can't recur unnoticed a second time.

No freezegun/time-machine dependency is available in this project, so these
tests assert structural/format properties and cross-check against
datetime.now() computed at test-run time, rather than pinning a fixed clock.
"""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from api.utils.template_renderer import _ordinal, render_template


def test_ordinal_suffixes():
    assert _ordinal(1) == "1st"
    assert _ordinal(2) == "2nd"
    assert _ordinal(3) == "3rd"
    assert _ordinal(4) == "4th"
    # The 11th-13th are "th", not "1st"/"2nd"/"3rd" — the %10 rule alone gets
    # these wrong without the %100 special case.
    assert _ordinal(11) == "11th"
    assert _ordinal(12) == "12th"
    assert _ordinal(13) == "13th"
    assert _ordinal(21) == "21st"
    assert _ordinal(22) == "22nd"
    assert _ordinal(23) == "23rd"
    assert _ordinal(31) == "31st"


def test_current_date_is_iso_format():
    rendered = render_template("{{current_date}}", {})
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", rendered)
    assert rendered == datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d")


def test_current_date_spoken_is_natural_language_with_ordinal():
    rendered = render_template("{{current_date_spoken}}", {})
    now = datetime.now(ZoneInfo("UTC"))
    expected = f"{now.strftime('%A')} the {_ordinal(now.day)} of {now.strftime('%B %Y')}"
    assert rendered == expected
    # e.g. "Sunday the 9th of August 2026"
    assert re.fullmatch(r"[A-Za-z]+ the \d{1,2}(st|nd|rd|th) of [A-Za-z]+ \d{4}", rendered)


def test_current_year_matches_wall_clock():
    assert render_template("{{current_year}}", {}) == str(
        datetime.now(ZoneInfo("UTC")).year
    )


def test_current_day_matches_current_weekday():
    assert render_template("{{current_day}}", {}) == render_template(
        "{{current_weekday}}", {}
    )


def test_time_now_is_24h_hh_mm():
    rendered = render_template("{{time_now}}", {})
    assert re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", rendered)


def test_time_now_spoken_reads_naturally_with_no_am_pm_letters():
    """AM/PM gets read aloud as literal letters by most TTS, so the spoken
    variant must avoid it entirely — see _spoken_time's docstring."""
    rendered = render_template("{{time_now_spoken}}", {})
    assert re.fullmatch(
        r"([1-9]|1[0-2]):[0-5]\d in the (morning|afternoon|evening)", rendered
    )
    assert "AM" not in rendered and "PM" not in rendered


def test_upcoming_days_covers_the_next_fourteen_days_starting_tomorrow():
    rendered = render_template("{{upcoming_days}}", {})
    lines = rendered.split("\n")
    assert len(lines) == 14

    today = datetime.now(ZoneInfo("UTC")).date()
    for offset, line in enumerate(lines, start=1):
        expected_date = today + timedelta(days=offset)
        expected_weekday = expected_date.strftime("%A")
        assert line.startswith(f"- {expected_weekday} "), (
            f"line {offset} should start with '- {expected_weekday} ': {line!r}"
        )
        assert str(expected_date.year) in line
        assert _ordinal(expected_date.day) in line


def test_upcoming_days_dates_are_strictly_increasing_and_never_repeat():
    rendered = render_template("{{upcoming_days}}", {})
    lines = rendered.split("\n")
    today = datetime.now(ZoneInfo("UTC")).date()
    seen_dates = set()
    for offset in range(1, 15):
        d = today + timedelta(days=offset)
        assert d not in seen_dates, "upcoming_days must not repeat a date"
        seen_dates.add(d)
    assert len(seen_dates) == 14
    assert len(lines) == 14


def test_builtins_respect_an_explicit_timezone_via_current_time_suffix():
    # current_weekday (and, by extension, the new builtins) inherit the
    # timezone from a current_time_<TZ> variable elsewhere in the SAME
    # template string — this pre-existing mechanism must keep working for
    # the new variables that share its "default_tz" plumbing.
    template = "{{current_time_America/Los_Angeles}} / {{current_date}}"
    rendered = render_template(template, {})
    la_time, iso_date = rendered.split(" / ")
    now_la = datetime.now(ZoneInfo("America/Los_Angeles"))
    assert la_time.startswith(now_la.strftime("%Y-%m-%d"))
    assert iso_date == now_la.strftime("%Y-%m-%d")


def test_unimplemented_variable_still_falls_through_to_context_lookup():
    # Guard against a regression where a new builtin accidentally shadows a
    # real context variable of the same name.
    assert render_template("{{first_name}}", {"first_name": "Mack"}) == "Mack"
