"""Recover overdue parallel offers after an API worker crash/restart.

Also drives the extra-answer chain (Dialer Phase 1b): teammate offers nobody joined
within 8 s move on, and agent calls get their 90 s transfer-back window.
"""
import asyncio
from typing import Any

from loguru import logger

from api.constants import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
from api.services.telephony.dialer import extra_answer
from api.services.telephony.providers.twilio.dialer_overflow_recovery import recover_overflow_offers


async def _drive_extra_answer(job: dict[str, Any]) -> None:
    try:
        async with asyncio.timeout(9):
            if job.get("provider") == "twilio":
                from api.services.telephony.providers.twilio import dialer_extra_answer
                if job.get("work") == "offer_expired":
                    await dialer_extra_answer.continue_after(
                        str(job["prospect_call_id"]), after="teammate",
                        account_sid=str(job.get("account_sid") or "") or None,
                    )
                elif job.get("work") == "agent":
                    await dialer_extra_answer.drive_agent(job)
            elif job.get("provider") == "signalwire" and job.get("work") == "offer_expired":
                from api.services.telephony.dialer import signalwire_extra_answer
                await signalwire_extra_answer.continue_after(str(job["prospect_call_id"]), after="teammate")
    except Exception as exc:  # noqa: BLE001 - the lease lapses and the next tick retries
        logger.warning(f"Extra-answer work deferred: {type(exc).__name__}")
    finally:
        await extra_answer.release_lease(str(job.get("id") or ""))


async def reconcile_extra_answers(limit: int = 20) -> None:
    jobs = await extra_answer.claim_work(limit)
    await asyncio.gather(*(_drive_extra_answer(job) for job in jobs))


async def reconcile_dialer_overflow(_ctx) -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return
    try:
        await recover_overflow_offers()
    except Exception as exc:
        logger.warning(f"Parallel overflow reconciliation deferred: {type(exc).__name__}")
    try:
        await reconcile_extra_answers()
    except Exception as exc:
        logger.warning(f"Extra-answer reconciliation deferred: {type(exc).__name__}")
