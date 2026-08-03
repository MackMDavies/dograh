"""Tests for classify_llm_exhaustion — provider-agnostic rate-limit/quota
error detection feeding the on_pipeline_error alerting path."""

from api.services.pipecat.llm_error_classification import classify_llm_exhaustion


class FakeStatusCodeError(Exception):
    def __init__(self, status_code, message="Rate limit reached"):
        super().__init__(message)
        self.status_code = status_code


class FakeCodeError(Exception):
    def __init__(self, code, message="quota exceeded"):
        super().__init__(message)
        self.code = code


class TestClassifyLlmExhaustion:
    def test_none_exception_returns_none(self):
        assert classify_llm_exhaustion(None) is None

    def test_unrelated_exception_returns_none(self):
        assert classify_llm_exhaustion(ValueError("bad input")) is None

    def test_matches_on_status_code_429(self):
        result = classify_llm_exhaustion(FakeStatusCodeError(429))
        assert result is not None
        assert result["reason"] == "rate_limited_or_quota_exceeded"
        assert result["matched_on"] == "status_code"
        assert result["exception_type"] == "FakeStatusCodeError"

    def test_matches_on_code_429_as_string(self):
        result = classify_llm_exhaustion(FakeCodeError("429"))
        assert result is not None
        assert result["matched_on"] == "code"

    def test_unrelated_status_code_does_not_match(self):
        assert classify_llm_exhaustion(FakeStatusCodeError(500, "server error")) is None

    def test_matches_on_message_keyword_when_no_status_code(self):
        result = classify_llm_exhaustion(Exception("Your project has exceeded its quota"))
        assert result is not None
        assert result["matched_on"] == "message"

    def test_matches_resource_exhausted_message(self):
        result = classify_llm_exhaustion(Exception("RESOURCE_EXHAUSTED: too many requests"))
        assert result is not None

    def test_long_message_is_truncated(self):
        result = classify_llm_exhaustion(Exception("quota " + "x" * 1000))
        assert result is not None
        assert len(result["exception_message"]) <= 500
