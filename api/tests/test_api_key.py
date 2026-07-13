import hashlib

import pytest

from api.services.configuration.check_validity import UserConfigurationValidator
from api.utils.api_key import generate_api_key, hash_api_key


def test_generate_api_key_uses_sys_prefix():
    raw, key_hash, key_prefix = generate_api_key()

    assert raw.startswith("sys_"), f"expected sys_ prefix, got {raw[:8]!r}"
    assert not raw.startswith("dgr_")


def test_generate_api_key_prefix_is_first_eight_chars():
    raw, key_hash, key_prefix = generate_api_key()

    assert key_prefix == raw[:8]
    assert len(key_prefix) == 8
    assert key_prefix.startswith("sys_")


def test_generate_api_key_hash_matches_sha256_of_full_key():
    raw, key_hash, key_prefix = generate_api_key()

    assert key_hash == hashlib.sha256(raw.encode()).hexdigest()


def test_generate_api_key_is_unique_per_call():
    raw1, _, _ = generate_api_key()
    raw2, _, _ = generate_api_key()

    assert raw1 != raw2


def test_hash_api_key_is_stable_for_existing_dgr_keys():
    # Backcompat: already-issued dgr_ keys must keep hashing to the same value,
    # so they continue to validate after the rebrand.
    legacy_key = "dgr_ExampleLegacyRawKeyValue1234567890abcd"

    assert hash_api_key(legacy_key) == hashlib.sha256(legacy_key.encode()).hexdigest()


def test_service_key_guard_rejects_new_sys_key():
    validator = UserConfigurationValidator()

    with pytest.raises(ValueError) as exc:
        validator._check_dograh_api_key("some-model", "sys_pastedAccountKey123")

    assert "service key" in str(exc.value).lower()


def test_service_key_guard_still_rejects_legacy_dgr_key():
    validator = UserConfigurationValidator()

    with pytest.raises(ValueError):
        validator._check_dograh_api_key("some-model", "dgr_pastedAccountKey123")
