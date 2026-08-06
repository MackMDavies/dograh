"""Voice selector must show one entry per real voice.

Two ways duplicates reach the picker:
  1. Duplicate rows — the provider sync does a check-then-insert with no unique
     constraint behind it, so two concurrent syncs both insert.
  2. Superusers list voices across ALL orgs, so the same provider voice synced
     into several orgs appears once per org.
"""

from types import SimpleNamespace

from api.services.voice_library.dedupe import dedupe_voices


def _v(id, provider, provider_voice_id, organization_id=1):
    return SimpleNamespace(
        id=id,
        provider=provider,
        provider_voice_id=provider_voice_id,
        organization_id=organization_id,
    )


def test_collapses_duplicate_rows_of_the_same_provider_voice():
    voices = [
        _v(4286, "elevenlabs", "QAg5vr34VHswVNMk2mjq"),
        _v(4287, "elevenlabs", "QAg5vr34VHswVNMk2mjq"),
    ]
    assert [v.id for v in dedupe_voices(voices)] == [4286]


def test_keeps_the_oldest_row_so_the_id_stays_stable():
    voices = [
        _v(999, "elevenlabs", "abc"),
        _v(12, "elevenlabs", "abc"),
    ]
    assert [v.id for v in dedupe_voices(voices)] == [12]


def test_collapses_the_same_voice_synced_into_different_orgs():
    """A superuser sees every org's copy; the picker should still show one."""
    voices = [
        _v(10, "elevenlabs", "abc", organization_id=1),
        _v(20, "elevenlabs", "abc", organization_id=7),
    ]
    assert [v.id for v in dedupe_voices(voices)] == [10]


def test_different_providers_sharing_an_id_are_not_collapsed():
    voices = [
        _v(1, "elevenlabs", "abc"),
        _v(2, "cartesia", "abc"),
    ]
    assert [v.id for v in dedupe_voices(voices)] == [1, 2]


def test_blank_provider_voice_id_rows_are_never_collapsed():
    """Clones in flight have no provider id yet — collapsing them would hide
    every pending clone but one."""
    voices = [
        _v(1, "dograh_clone", ""),
        _v(2, "dograh_clone", ""),
        _v(3, "dograh_clone", None),
    ]
    assert [v.id for v in dedupe_voices(voices)] == [1, 2, 3]


def test_preserves_input_ordering():
    voices = [
        _v(5, "cartesia", "z"),
        _v(1, "elevenlabs", "a"),
        _v(3, "elevenlabs", "a"),
    ]
    assert [v.id for v in dedupe_voices(voices)] == [5, 1]


def test_empty_input():
    assert dedupe_voices([]) == []
