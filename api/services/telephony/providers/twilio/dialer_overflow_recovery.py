"""Restart-safe recovery of the existing prospect leg after an 8s rep offer."""
import asyncio

from loguru import logger

from api.services.telephony.providers.twilio import dialer_agent_handoff
from api.services.telephony.providers.twilio.dialer_conference import _twilio_client, cancel_call


async def schedule_overflow(*, parent_call_sid: str, child_call_sid: str, leg_id: str, account_sid: str) -> bool:
    return await dialer_agent_handoff._rpc("dialer_schedule_parallel_overflow", {
        "p_parent_call_sid":parent_call_sid,"p_child_call_sid":child_call_sid,
        "p_leg_id":leg_id,"p_account_sid":account_sid,
    }) is True


async def recover_overflow_offers(*, child_call_sid: str | None = None) -> None:
    """Leases survive process loss; unacknowledged provider updates are retried.

    A stream that authenticated before an acknowledgement was lost is left
    running. Its stored run is reused, never restarted or redialled.
    """
    jobs = await dialer_agent_handoff._rpc("dialer_claim_parallel_overflow_jobs", {"p_child_call_sid":child_call_sid})
    if not isinstance(jobs,list):
        return
    # Process each claimed deadline concurrently so the last job cannot wait
    # behind nineteen carrier requests until its own lease has already expired.
    await asyncio.gather(*(_recover_job(job) for job in jobs))


async def _recover_job(job: dict) -> None:
    try:
        # Stay inside the database's 30s lease, allowing for the claim response
        # and a carrier thread's own 5s timeout after cancellation of its await.
        async with asyncio.timeout(12):
            client = await _twilio_client()
            if not client or client.account_sid != job["account_sid"]:
                raise dialer_agent_handoff.AgentHandoffUnavailable("The original Twilio account is unavailable")
            call = await asyncio.to_thread(client.calls(job["child_call_sid"]).fetch)
            if call.status in {"queued","ringing","in-progress"}:
                try:
                    prepared = await dialer_agent_handoff.prepare_agent_handoff(
                        parent_call_sid=job["parent_call_sid"],child_call_sid=job["child_call_sid"],
                        leg_id=job["leg_id"],account_sid=job["account_sid"],allow_fallback=True,
                    )
                except dialer_agent_handoff.AgentHandoffUnavailable as exc:
                    if exc.retryable:
                        raise
                    prepared = None
                if prepared:
                    if not prepared.already_streaming:
                        await asyncio.to_thread(client.calls(job["child_call_sid"]).update,twiml=prepared.twiml)
                elif not await cancel_call(call_sid=job["child_call_sid"]):
                    raise RuntimeError("Could not end the unroutable overflow line")
            elif call.status not in {"completed","busy","failed","no-answer","canceled"}:
                raise RuntimeError("Unknown original call state")
            await dialer_agent_handoff._rpc("dialer_finish_parallel_overflow_job", {
                "p_child_call_sid":job["child_call_sid"],"p_lease_token":job["lease_token"],
            })
    except Exception as exc:
        # Keep the lease unfinished. A new worker can retry after expiry.
        logger.warning(f"Parallel overflow recovery deferred: {type(exc).__name__}")
