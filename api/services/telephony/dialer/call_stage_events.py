"""Stage recorder seam for the call sorter (live listening stream).

call_sorter discovers ``record_call_stage`` here. It writes to
``dialer_call_stage_events`` through call_stages: the sorter's verdicts
('screener', 'ivr', 'mailbox_full', 'machine', 'person') go through
``report_listening_stage`` (so a full mailbox also fails a queued/playing Twilio
voicemail); any other known stage (e.g. 'beep') is recorded as is. Never raises.
"""

from loguru import logger

from api.services.telephony.dialer import call_stages


async def record_call_stage(call_ref: str, stage: str, detail: dict) -> None:
    try:
        if stage in call_stages.LISTENING_STAGES:
            await call_stages.report_listening_stage(call_key=call_ref, stage=stage, detail=detail)
        else:
            await call_stages.record_call_stage(call_ref, stage, {"source": "listening", **(detail or {})})
    except Exception as exc:  # noqa: BLE001 - sorting evidence never breaks a call
        logger.info(f"Could not record sorter stage {stage} for {call_ref}: {type(exc).__name__}")
