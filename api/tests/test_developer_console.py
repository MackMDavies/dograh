"""Developers console: request-log classification, Langfuse config and span options.

Pure-function tests (no DB, no network).
"""

from api.services.api_request_logging import (
    classify_surface,
    extract_api_key,
    mcp_method_of,
)
from api.services.telemetry.langfuse_config import (
    DEFAULT_OPTIONS,
    LEGACY_OPTIONS,
    decode_langfuse_config,
    encode_langfuse_config,
    normalize_options,
    validate_host,
)
from api.services.telemetry.span_filter import filter_span, redact_text, span_kind


def test_classify_surface():
    assert classify_surface("/api/v1/public/agent/abc") == "public"
    assert classify_surface("/api/v1/mcp") == "mcp"
    assert classify_surface("/api/v1/mcp/") == "mcp"
    assert classify_surface("/api/v1/workflow/fetch") == "rest"
    assert classify_surface("/api/v1/health") is None
    assert classify_surface("/static/x") is None
    # A REST path that merely starts with "mcp" is not the MCP mount.
    assert classify_surface("/api/v1/mcpx") == "rest"


def test_extract_api_key_ignores_jwts():
    assert extract_api_key({"x-api-key": "sys_abc"}) == "sys_abc"
    assert extract_api_key({"authorization": "Bearer sys_abc"}) == "sys_abc"
    assert extract_api_key({"authorization": "Bearer aaa.bbb.ccc"}) is None
    assert extract_api_key({}) is None


def test_mcp_method_labels():
    assert mcp_method_of(b'{"jsonrpc":"2.0","method":"tools/list"}') == "tools/list"
    assert (
        mcp_method_of(b'{"method":"tools/call","params":{"name":"list_workflows"}}')
        == "tools/call list_workflows"
    )
    assert mcp_method_of(b'[{"method":"initialize"}]') == "initialize"
    assert mcp_method_of(b"not json") is None


def test_langfuse_config_round_trip_encrypts_secret():
    stored = encode_langfuse_config("https://cloud.langfuse.com/", "pk-lf-1", "sk-lf-2", {"redact": False})
    assert "secret_key" not in stored and stored["secret_key_enc"] != "sk-lf-2"
    decoded = decode_langfuse_config(stored)
    assert decoded["secret_key"] == "sk-lf-2"
    assert decoded["host"] == "https://cloud.langfuse.com"
    assert decoded["options"]["redact"] is False
    assert decoded["options"]["traces"] is True


def test_legacy_plaintext_rows_keep_exporting_everything():
    decoded = decode_langfuse_config({"host": "h", "public_key": "p", "secret_key": "s"})
    assert decoded["secret_key"] == "s"
    assert decoded["options"] == LEGACY_OPTIONS


def test_normalize_options_defaults():
    assert normalize_options(None) == DEFAULT_OPTIONS
    assert normalize_options({"transcripts": 1})["transcripts"] is True


def test_validate_host_rejects_non_https_and_private():
    assert validate_host("http://cloud.langfuse.com") is not None
    assert validate_host("https://127.0.0.1") is not None
    assert validate_host("https://10.0.0.5") is not None


def test_span_kinds():
    assert span_kind("conversation", {}) == "trace"
    assert span_kind("turn-3", {}) == "trace"
    assert span_kind("llm", {}) == "model"
    assert span_kind("knowledge_base_retrieval", {}) == "tool"
    assert span_kind("whatever", {"gen_ai.request.model": "x"}) == "model"


def test_filter_span_options():
    opts = dict(DEFAULT_OPTIONS)
    # transcripts off: STT text and transcript attributes are stripped, span kept
    out = filter_span("stt", {"gen_ai.request.model": "x", "output": "hello", "transcript": "hi"}, opts)
    assert out == {"gen_ai.request.model": "x"}
    # traces off drops the conversation skeleton
    assert filter_span("conversation", {}, {**opts, "traces": False}) is None
    # tools off drops tool spans and tool attributes on LLM spans
    assert filter_span("knowledge_base_retrieval", {}, {**opts, "tools": False}) is None
    out = filter_span("llm", {"gen_ai.x": 1, "tools.names": "a", "function_calls.count": 1}, {**opts, "tools": False})
    assert out == {"gen_ai.x": 1}
    # spans off drops model spans
    assert filter_span("llm", {}, {**opts, "spans": False}) is None


def test_redaction():
    text = "Call +44 7700 900123, mail jo@example.com, card 4242 4242 4242 4242 on 2026-10-25"
    out = redact_text(text)
    assert "[phone]" in out and "[email]" in out and "[card]" in out
    assert "2026-10-25" in out
    out = filter_span("llm", {"gen_ai.x": 1, "input": "my email is a@b.co"}, {**DEFAULT_OPTIONS, "redact": True})
    assert out["input"] == "my email is [email]"


async def test_middleware_times_request_and_replays_mcp_body(monkeypatch):
    import asyncio

    from api.services import api_request_logging as mod

    recorded = []

    async def fake_record(raw_key, row):
        recorded.append((raw_key, row))

    monkeypatch.setattr(mod, "_record", fake_record)
    seen_body = {}

    async def app(scope, receive, send):
        msg = await receive()
        seen_body["body"] = msg["body"]
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = mod.ApiRequestLogMiddleware(app)
    body = b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_workflow"}}'
    msgs = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return msgs.pop(0) if msgs else {"type": "http.disconnect"}

    sent = []

    async def send(m):
        sent.append(m)

    scope = {
        "type": "http",
        "path": "/api/v1/mcp/",
        "method": "POST",
        "headers": [(b"x-api-key", b"sys_test"), (b"user-agent", b"claude-code/2.1")],
    }
    await mw(scope, receive, send)
    await asyncio.sleep(0)
    assert seen_body["body"] == body
    assert sent[0]["status"] == 202
    raw, row = recorded[0]
    assert raw == "sys_test"
    assert row["surface"] == "mcp" and row["status"] == 202
    assert row["mcp_method"] == "tools/call get_workflow"
    assert row["user_agent"] == "claude-code/2.1"


async def test_middleware_skips_session_traffic(monkeypatch):
    import asyncio

    from api.services import api_request_logging as mod

    recorded = []

    async def fake_record(raw_key, row):
        recorded.append(row)

    monkeypatch.setattr(mod, "_record", fake_record)

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})

    mw = mod.ApiRequestLogMiddleware(app)
    scope = {"type": "http", "path": "/api/v1/workflow/fetch", "method": "GET",
             "headers": [(b"authorization", b"Bearer aaa.bbb.ccc")]}

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(m):
        pass

    await mw(scope, receive, send)
    await asyncio.sleep(0)
    assert recorded == []


def test_routes_and_tracing_import():
    import api.routes.developer_console  # noqa: F401
    import api.routes.organization  # noqa: F401
    import api.services.pipecat.tracing_config as tc

    assert tc._apply_send_options([], {"traces": True}) == []
