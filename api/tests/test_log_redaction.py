from api.logging_config import redact_secrets


def test_redacts_the_listen_token_uvicorn_logged():
    # The shape of the 2026-09-24 access-log line (token shortened).
    line = (
        '99.251.247.144:0 - "WebSocket /api/v1/telephony/sw-listen?call_id=d7b1&amp;'
        'token=eyJhbGciOi.eyJzdWIi.sig-_x&amp;captions_only=1" [accepted]'
    )
    out = redact_secrets(line)
    assert "eyJ" not in out
    assert "token=[redacted]" in out
    assert "call_id=d7b1" in out and "captions_only=1" in out


def test_redacts_the_webhook_secret_and_plain_ampersands():
    out = redact_secrets("POST /api/v1/telephony/sw-call-status?call_id=x&k=s3cr3t HTTP/1.1")
    assert "s3cr3t" not in out
    assert "k=[redacted]" in out


def test_leaves_ordinary_parameters_alone():
    line = 'GET /rest/v1/dialer_calls?select=id&parent_call_sid=eq.b78f&limit=1 "HTTP/1.1 200 OK"'
    assert redact_secrets(line) == line
