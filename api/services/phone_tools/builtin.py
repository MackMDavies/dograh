"""Built-in phone tools the model can call: texting the caller, pausing the recording.

press_keys (the keypad) predates this module and lives in pipecat_engine.py. Each tool
here is offered only when the organization allows it (policy.py) and the call can
support it: a text needs the other party's number and the Sysevo SMS hook; pausing
the recording needs the call's audio recorder.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx
from loguru import logger

SEND_TEXT = "send_text"
SET_RECORDING = "set_recording"

MAX_TEXT_CHARS = 600
# A caller asking for "the link and the address" is two texts; more is a loop.
MAX_TEXTS_PER_CALL = 3
_TIMEOUT = 15.0

SEND_TEXT_DESCRIPTION = (
    "Send a text message to the caller's phone while you are still talking: a link, "
    "a booking confirmation, an address or directions. Only send it after the caller "
    "has agreed to receive a text. Keep it short and include everything they need, "
    "because they cannot reply to you through it during the call. After it is sent, "
    "tell them it is on its way."
)
SEND_TEXT_PROPERTIES = {
    "message": {
        "type": "string",
        "description": f"The text to send, at most {MAX_TEXT_CHARS} characters.",
    }
}

SET_RECORDING_DESCRIPTION = (
    "Pause or resume the call recording. Pause it as soon as the caller asks not to be "
    "recorded, and resume it only when they agree to be recorded. The written "
    "transcript of the call is still kept while the recording is paused."
)
SET_RECORDING_PROPERTIES = {
    "recording": {
        "type": "string",
        "enum": ["off", "on"],
        "description": "'off' to pause the recording, 'on' to resume it.",
    }
}


def sms_hook_url() -> Optional[str]:
    """SYSEVO_AGENT_SMS_URL, else the dograh-agent-sms function next to the live-call
    hook (both are Sysevo edge functions on the same project)."""
    explicit = os.getenv("SYSEVO_AGENT_SMS_URL")
    if explicit:
        return explicit
    live = os.getenv("SYSEVO_CALL_LIVE_URL") or ""
    if live.rstrip("/").endswith("/dograh-call-live"):
        return live.rstrip("/")[: -len("dograh-call-live")] + "dograh-agent-sms"
    return None


def clean_text(message: Any) -> str:
    return " ".join(str(message or "").split())[:MAX_TEXT_CHARS]


async def send_caller_text(
    *, workflow_id: Optional[int], workflow_run_id: Optional[int], to: str, body: str
) -> dict[str, Any]:
    """Hand the text to Sysevo (credits, opt-outs, sender number and the inbox thread
    all live there). Returns {status, reason?, readable_reason?}; never raises."""
    url = sms_hook_url()
    secret = os.getenv("SYSEVO_MEMORY_SECRET", "")
    if not url or not secret:
        return {"status": "failed", "reason": "not_configured",
                "readable_reason": "Texting is not set up on this line."}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                url,
                json={
                    "workflow_id": workflow_id,
                    "workflow_run_id": workflow_run_id,
                    "to": to,
                    "body": body,
                },
                headers={"X-Sysevo-Secret": secret},
            )
        data = response.json() if response.content else {}
    except Exception as exc:  # noqa: BLE001 -- a failed text must not break the call
        logger.warning(f"[send_text] hook failed for run {workflow_run_id}: {exc}")
        return {"status": "failed", "reason": "send_error",
                "readable_reason": "The text could not be sent."}
    if not response.is_success:
        return {"status": "failed", "reason": str(data.get("error") or response.status_code),
                "readable_reason": "The text could not be sent."}
    return {
        "status": str(data.get("status") or "failed"),
        "reason": data.get("reason"),
        "readable_reason": data.get("readableReason"),
    }
