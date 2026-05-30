from api.schemas.voice_library import (
    VoiceLibraryCreateSchema,
    VoiceLibraryUpdateSchema,
    VoiceLibraryResponseSchema,
    ElevenLabsImportRequestSchema,
)

def test_create_schema_requires_name():
    s = VoiceLibraryCreateSchema(name="My Voice")
    assert s.name == "My Voice"
    assert s.is_public is False

def test_update_schema_all_optional():
    s = VoiceLibraryUpdateSchema()
    assert s.name is None

def test_import_schema():
    s = ElevenLabsImportRequestSchema(voice_ids=["abc", "def"])
    assert len(s.voice_ids) == 2
