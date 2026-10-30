import pytest

from api.services.telephony.dialer.live_transcribe import _DEEPGRAM_URL, correct_brand_spelling

_HEARD = ["Sasebo", "Saseo", "Saseevo", "Sasivo", "Siveo", "Cecebo", "Sysavo", "Sysivo", "Syseevo",
          "Sisevo", "Cisevo", "Sasavo", "Seesevo", "Sizevo", "sysevo", "Sys evo", "Sai sevo", "Sigh-seevo"]


@pytest.mark.parametrize("heard", _HEARD)
def test_mis_heard_names_become_sysevo(heard):
    assert correct_brand_spelling(f"we have a demo from {heard} for you") == "we have a demo from Sysevo for you"


def test_a_real_company_name_only_after_calling_from():
    assert correct_brand_spelling("Hi. It's Matt calling from Sysco.") == "Hi. It's Matt calling from Sysevo."
    assert correct_brand_spelling("We use Cisco phones and buy from Sysco.") == "We use Cisco phones and buy from Sysco."


def test_ordinary_words_are_untouched():
    text = ("So we said the system saves you time, says Sam, in this season. Sizable, civic, Cicero, "
            "seesaw, saves, sieve, Silvia, service, cesspool, Savoy, sisal, Seville, crisis, oasis, Caesar, Cisco.")
    assert correct_brand_spelling(text) == text


def test_nova3_is_told_to_expect_the_name():
    assert "model=nova-3" in _DEEPGRAM_URL and "keyterm=Sysevo" in _DEEPGRAM_URL
    assert "keywords=" not in _DEEPGRAM_URL  # nova-3 ignores keywords
