from api.schemas.voice_library import FishCatalogVoiceSchema, FishPublicVoiceSchema


class TestFishSchemas:
    def test_fish_catalog_voice_schema_minimal(self):
        v = FishCatalogVoiceSchema(voice_id="abc123", name="My Voice")
        assert v.voice_id == "abc123"
        assert v.name == "My Voice"
        assert v.languages == []

    def test_fish_public_voice_schema_full(self):
        v = FishPublicVoiceSchema(
            voice_id="pub123",
            name="Narrator",
            languages=["en"],
            tags=["calm"],
            preview_url="https://example.com/sample.mp3",
            author_nickname="someone",
        )
        assert v.languages == ["en"]
        assert v.tags == ["calm"]
        assert v.author_nickname == "someone"
