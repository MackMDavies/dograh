"""Per-request log of organisation-API-key traffic for the Developers console.

Pure ASGI middleware: it never reads or alters responses, only times them. For every
request under ``/api/v1`` that carries an organisation API key (``X-API-Key`` or a
non-JWT ``Authorization: Bearer``) it records surface (REST / Public Agent API / Voice
workflow MCP), method, path, JSON-RPC method + tool name for MCP calls, status,
latency and a truncated user agent. No bodies, no credentials, no query values.

Rows are buffered in memory and inserted in batches every couple of seconds so the
request path never waits on the database. Any failure here is swallowed: logging must
never affect an API call. Rows older than ``API_REQUEST_LOG_RETENTION_DAYS`` (default
30) are purged by the ``purge_api_request_logs`` ARQ cron.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from api.utils.api_key import hash_api_key

API_PREFIX = "/api/v1"
RETENTION_DAYS = int(os.getenv("API_REQUEST_LOG_RETENTION_DAYS", "30") or 30)
_FLUSH_EVERY_S = 2.0
_MAX_BUFFER = 5000
_MAX_MCP_BODY = 64 * 1024
_IDENTITY_TTL_S = 300.0

_buffer: List[Dict[str, Any]] = []
_flusher: Optional[asyncio.Task] = None
_identity_cache: Dict[str, Tuple[float, Optional[Tuple[int, int]]]] = {}


def classify_surface(path: str) -> Optional[str]:
    """'public' | 'mcp' | 'rest' for an /api/v1 path, else None (not logged)."""
    if not path.startswith(API_PREFIX):
        return None
    rest = path[len(API_PREFIX):]
    if rest.startswith("/public/agent"):
        return "public"
    if rest == "/mcp" or rest.startswith("/mcp/"):
        return "mcp"
    if rest.startswith("/health"):
        return None
    return "rest"


def extract_api_key(headers: Dict[str, str]) -> Optional[str]:
    """The organisation API key on a request, or None for JWT / anonymous traffic."""
    key = headers.get("x-api-key")
    if key:
        return key.strip() or None
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        # Supabase / OSS sessions are JWTs (three dot-separated parts); API keys are not.
        if token and token.count(".") != 2:
            return token
    return None


def mcp_method_of(body: bytes) -> Optional[str]:
    """'tools/call list_workflows' style label from a JSON-RPC body (batch: first item)."""
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    method = data.get("method")
    if not isinstance(method, str):
        return None
    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    name = params.get("name") if isinstance(params, dict) else None
    label = f"{method} {name}" if method == "tools/call" and isinstance(name, str) else method
    return label[:160]


async def _identity(raw_key: str) -> Optional[Tuple[int, int]]:
    h = hash_api_key(raw_key)
    now = time.monotonic()
    hit = _identity_cache.get(h)
    if hit and now - hit[0] < _IDENTITY_TTL_S:
        return hit[1]
    from api.db import db_client  # lazy: keep import graph light on startup

    ident = await db_client.get_api_key_identity_by_hash(h)
    if len(_identity_cache) > 2000:
        _identity_cache.clear()
    _identity_cache[h] = (now, ident)
    return ident


async def _flush_loop() -> None:
    from api.db import db_client

    while True:
        await asyncio.sleep(_FLUSH_EVERY_S)
        if not _buffer:
            continue
        batch = _buffer[:]
        del _buffer[: len(batch)]
        try:
            await db_client.insert_api_request_logs(batch)
        except Exception as e:  # never raise into the app
            logger.debug(f"[api-request-log] flush failed ({len(batch)} rows): {e}")


def _ensure_flusher() -> None:
    global _flusher
    if _flusher is None or _flusher.done():
        try:
            _flusher = asyncio.get_running_loop().create_task(_flush_loop())
        except RuntimeError:
            _flusher = None


async def _record(raw_key: str, row: Dict[str, Any]) -> None:
    try:
        ident = await _identity(raw_key)
        if not ident:
            return  # unknown key: cannot be attributed to an organisation
        row["api_key_id"], row["organization_id"] = ident
        if len(_buffer) < _MAX_BUFFER:
            _buffer.append(row)
        _ensure_flusher()
    except Exception as e:
        logger.debug(f"[api-request-log] record failed: {e}")


class ApiRequestLogMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        path: str = scope.get("path", "")
        surface = classify_surface(path)
        if not surface:
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        raw_key = extract_api_key(headers)
        if not raw_key:
            return await self.app(scope, receive, send)

        method = scope.get("method", "GET")
        mcp_label: Optional[str] = None
        if surface == "mcp" and method == "POST":
            # Buffer the (small) JSON-RPC body once, then replay it to the app.
            chunks: List[bytes] = []
            size = 0
            more = True
            first: List[Dict[str, Any]] = []
            while more:
                msg = await receive()
                if msg["type"] != "http.request":
                    first.append(msg)
                    break
                body = msg.get("body", b"")
                size += len(body)
                chunks.append(body)
                more = msg.get("more_body", False)
                if size > _MAX_MCP_BODY:
                    break
            full = b"".join(chunks)
            if not more:
                mcp_label = mcp_method_of(full)
            replay = [{"type": "http.request", "body": full, "more_body": more}] + first

            async def receive_replay():
                if replay:
                    return replay.pop(0)
                return await receive()

            receive_fn = receive_replay
        else:
            receive_fn = receive

        status_holder = {"status": 500}

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                status_holder["status"] = int(message.get("status", 500))
            await send(message)

        started = time.perf_counter()
        try:
            await self.app(scope, receive_fn, send_wrapper)
        finally:
            try:
                latency = int((time.perf_counter() - started) * 1000)
                row = {
                    "surface": surface,
                    "method": method[:8],
                    # Path only (ids identify the resource called); query strings dropped.
                    "path": path[:512],
                    "mcp_method": mcp_label,
                    "status": status_holder["status"],
                    "latency_ms": latency,
                    "user_agent": (headers.get("user-agent") or "")[:160] or None,
                }
                asyncio.get_running_loop().create_task(_record(raw_key, row))
            except Exception as e:
                logger.debug(f"[api-request-log] schedule failed: {e}")


async def purge_api_request_logs(ctx) -> None:
    """ARQ daily cron: enforce the request-log retention window."""
    from api.db import db_client

    try:
        n = await db_client.purge_api_request_logs(RETENTION_DAYS)
        if n:
            logger.info(f"[api-request-log] purged {n} rows older than {RETENTION_DAYS}d")
    except Exception as e:
        logger.warning(f"[api-request-log] purge failed: {e}")
