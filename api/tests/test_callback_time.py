from api.services.campaign.callback_time import resolve_timezone


def test_known_us_area_codes():
    assert resolve_timezone("+12125551234") == "America/New_York"   # NYC
    assert resolve_timezone("+13105551234") == "America/Los_Angeles" # LA
    assert resolve_timezone("+14025551234") == "America/Chicago"     # Nebraska
    assert resolve_timezone("+14035551234") == "America/Edmonton"    # Alberta


def test_uk_number():
    assert resolve_timezone("+441615551234") == "Europe/London"


def test_unmapped_area_code_returns_none():
    """None means 'send to review' — never silently fall back to UK and dial."""
    assert resolve_timezone("+19995551234") is None


def test_malformed_input_never_raises():
    for value in (None, "", "not a number", "+1", "12345"):
        assert resolve_timezone(value) is None
