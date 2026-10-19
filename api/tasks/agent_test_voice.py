"""ARQ entry point. Its only logged argument is a non-secret session UUID."""

import asyncio
import os

import httpx

from api.db import db_client
from api.services.workflow.voice_test_capacity import (
    claim_voice_capacity,
    release_voice_capacity,
)
from api.services.workflow.voice_test_control import (
    VoiceControlClient,
    VoiceControlError,
)
from api.services.workflow.voice_test_job import (
    VoiceJobContext,
    execute_voice_job,
    voice_job_key,
)


async def run_agent_test_voice(ctx, session_id):
    slot = None
    release_allowed = True
    try:
        async with asyncio.timeout(10):
            encoded = await ctx["redis"].get(voice_job_key(session_id))
        if encoded is None:
            return {"status": "blocked", "code": "voice_job_context_expired"}
        context = VoiceJobContext.parse(encoded, session_id)
        secret = os.getenv("SYSEVO_MEMORY_SECRET", "")
        async with httpx.AsyncClient() as http:
            control = VoiceControlClient(http, secret)
            slot = await claim_voice_capacity(ctx["redis"])
            release_allowed = False
            result = await execute_voice_job(db_client, control, context, secret)
            release_allowed = result.get("cleanup_verified") is True
            return result
    except asyncio.CancelledError:
        release_allowed = False
        raise
    except VoiceControlError as exc:
        return {"status": "blocked", "code": exc.code}
    except Exception:  # noqa: BLE001 - prevent ARQ from logging raw SDK errors or capabilities
        # Never let ARQ serialize/log exception arguments containing a lease,
        # HTTP body, provider credential or raw database response.
        return {"status": "blocked", "code": "voice_job_unavailable"}
    finally:
        if slot is not None and release_allowed:
            await release_voice_capacity(ctx["redis"], slot)
