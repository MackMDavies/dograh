"""Tests for the at-rest encryption TypeDecorators in api.services.crypto.

The pure TypeDecorator tests (roundtrip / legacy-plaintext / None) only need
`cryptography` + `sqlalchemy`. The column-binding tests import the ORM models.
"""
from api.services.crypto import EncryptedJSON, EncryptedString


def test_encrypted_string_roundtrip():
    t = EncryptedString()
    stored = t.process_bind_param("sk-secret-123", dialect=None)
    assert stored is not None and stored != "sk-secret-123"  # not plaintext
    assert t.process_result_value(stored, dialect=None) == "sk-secret-123"


def test_encrypted_string_tolerates_legacy_plaintext():
    # A pre-migration plaintext value must still read back unchanged.
    t = EncryptedString()
    assert t.process_result_value("sk-legacy-plaintext", dialect=None) == "sk-legacy-plaintext"


def test_encrypted_string_none_passthrough():
    t = EncryptedString()
    assert t.process_bind_param(None, dialect=None) is None
    assert t.process_result_value(None, dialect=None) is None


def test_encrypted_json_roundtrip_and_plaintext_fallback():
    t = EncryptedJSON()
    payload = {"account_sid": "AC123", "auth_token": "tok_secret"}
    stored = t.process_bind_param(payload, dialect=None)
    assert isinstance(stored, str) and "tok_secret" not in stored  # encrypted
    assert t.process_result_value(stored, dialect=None) == payload
    # legacy plaintext JSON string still parses
    assert t.process_result_value('{"account_sid": "AC9"}', dialect=None) == {"account_sid": "AC9"}
    assert t.process_bind_param(None, dialect=None) is None


def test_api_key_column_uses_encrypted_type():
    from api.db.models import OrgProviderConnectionModel

    col = OrgProviderConnectionModel.__table__.c.api_key
    assert isinstance(col.type, EncryptedString), "api_key must be EncryptedString at rest"


def test_telephony_credentials_column_uses_encrypted_type():
    from api.db.models import TelephonyConfigurationModel

    col = TelephonyConfigurationModel.__table__.c.credentials
    assert isinstance(col.type, EncryptedJSON), "credentials must be EncryptedJSON at rest"
