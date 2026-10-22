import pytest

from api.services.telephony.dialer.live_transcribe import _DEEPGRAM_URL, correct_brand_spelling


@pytest.mark.parametrize("heard", ["Sasebo", "Saseo", "Saseevo", "Sysavo", "Sysivo", "Sisevo", "Cisevo", "sysevo", "Sys evo"])
def test_mis_heard_names_become_sysevo(heard):
    assert correct_brand_spelling(f"calling from {heard} about the demo") == "calling from Sysevo about the demo"


def test_ordinary_words_are_untouched():
    text = "So we said the system saves you time, says Sam, in this season."
    assert correct_brand_spelling(text) == text


def test_the_name_is_boosted():
    assert "keywords=Sysevo%3A2" in _DEEPGRAM_URL
