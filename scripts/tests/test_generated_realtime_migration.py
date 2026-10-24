import unittest
from scripts.migrate_generated_agents_to_elevenlabs_realtime import realtime_configuration


class GeneratedRealtimeMigrationTests(unittest.TestCase):
    def test_preserves_instructions_guardrails_tools_and_auxiliary_models(self):
        original = {"guardrails": {"enabled": True}, "voice_expression": {"empathy": "supportive"},
                    "model_overrides": {"llm": {"provider": "openai"}, "tts": {"voice": "old"}}}
        result = realtime_configuration(original, "selected-voice")
        self.assertEqual(result["guardrails"], original["guardrails"])
        self.assertEqual(result["voice_expression"], original["voice_expression"])
        self.assertEqual(result["model_overrides"]["tts"], original["model_overrides"]["tts"])
        self.assertNotIn("is_realtime", original["model_overrides"])
        self.assertTrue(result["model_overrides"]["is_realtime"])
        self.assertEqual(result["model_overrides"]["realtime"]["voice"], "selected-voice")

    def test_drops_foreign_provider_credentials_and_endpoint(self):
        original = {"model_overrides": {"realtime": {"provider": "openai_realtime", "api_key": "foreign", "base_url": "https://foreign.invalid"}}}
        result = realtime_configuration(original, "voice")["model_overrides"]["realtime"]
        self.assertEqual(result, {"provider": "elevenlabs_realtime", "model": "eleven_realtime_v1_mini", "voice": "voice"})

    def test_preserves_existing_elevenlabs_settings_and_is_idempotent(self):
        original = {"model_overrides": {"realtime": {"provider": "elevenlabs_realtime", "language": "en"}}}
        result = realtime_configuration(original, "voice")
        self.assertEqual(result["model_overrides"]["realtime"]["language"], "en")
        self.assertEqual(realtime_configuration(result, "voice"), result)


if __name__ == "__main__":
    unittest.main()
