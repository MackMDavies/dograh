from unittest.mock import MagicMock, patch

from api.services.configuration.check_validity import UserConfigurationValidator


class TestFishApiKeyValidation:
    def test_valid_key_returns_true(self):
        validator = UserConfigurationValidator()
        mock_response = MagicMock(status_code=200)
        mock_response.raise_for_status.return_value = None
        with patch(
            "api.services.configuration.check_validity.httpx.get",
            return_value=mock_response,
        ) as mock_get:
            result = validator._check_fish_api_key("s2-pro", "test-key")

        assert result is True
        args, kwargs = mock_get.call_args
        assert args[0] == "https://api.fish.audio/model"
        assert kwargs["headers"] == {"Authorization": "Bearer test-key"}
        assert kwargs["params"] == {"self": "true", "page_size": 1}

    def test_invalid_key_raises_value_error(self):
        import httpx

        validator = UserConfigurationValidator()
        mock_response = MagicMock(status_code=401)
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Unauthorized", request=MagicMock(), response=mock_response
        )
        with patch(
            "api.services.configuration.check_validity.httpx.get",
            return_value=mock_response,
        ):
            try:
                validator._check_fish_api_key("s2-pro", "bad-key")
                assert False, "expected ValueError"
            except ValueError as e:
                assert "Fish Audio" in str(e)
