"""Syra's voice calls — runs that are an assistant talking to its own user, not a call.

Sysevo's assistant Syra speaks over this pipeline (spec:
docs/superpowers/specs/2026-09-23-syra-voice-over-dograh-design.md in the Sysevo repo).
Her workflow carries ``workflow_configurations.syra_voice = true`` and its ID is registered
in the service-owned Syra catalog. The flag alone grants no exemption. A run of that workflow
is a person talking to their own assistant, so it must NOT be treated as a customer call:

  - no wallet check and no concurrency slot at embed init (it is not billable voice, and
    it must not starve a campaign of its org's slots);
  - no "call is live" webhook and no caller-memory pre-call fetch at pipeline start (it
    would show on the live-calls board and look the person up as a caller);
  - no QA analysis, caller-memory, wallet-debit or call-ended webhooks at completion
    (they would create call_analysis rows, bill minutes and write caller memory).

Everything else — STT, VAD, turn-taking, interruption, TTS — is the ordinary pipeline;
that is the point of running her here.
"""

import math
from typing import Any, Optional


def syra_speech_speed(value: Any) -> Optional[float]:
    """Return a safe per-call ElevenLabs speed, or None to keep the workflow default."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    speed = float(value)
    if not math.isfinite(speed):
        return None
    return max(0.85, min(1.2, speed))


def is_syra_voice_config(workflow_configurations: Optional[dict[str, Any]]) -> bool:
    """A candidate marker only; billing also requires service-owned registration."""
    return bool(workflow_configurations) and workflow_configurations.get("syra_voice") is True


async def is_syra_voice_workflow(workflow_id: Optional[int], *, db=None, client=None) -> bool:
    """Looks the workflow up. Any failure answers False: a real call must never be
    exempted from billing because a lookup broke."""
    if type(workflow_id) is not int or not 0 < workflow_id <= 2147483647:
        return False
    try:
        if db is None:
            from api.db import db_client
            db = db_client
        workflow = await db.get_workflow_by_id(workflow_id)
        if getattr(workflow, 'id', None) != workflow_id or not is_syra_voice_config(getattr(workflow, "workflow_configurations", None)):
            return False
        # A customer can edit its workflow configuration. Only the platform's
        # service-owned Syra registry can authorize a billing exemption.
        from api.services.subscription_call_budget import configured_budget_client
        billing = client if client is not None else configured_budget_client()
        return await billing.attest_assistant(workflow_id) is True
    except Exception:
        return False


async def is_syra_voice_run(workflow_run_id: int) -> bool:
    try:
        from api.db import db_client

        run = await db_client.get_workflow_run(workflow_run_id)
        return await is_syra_voice_workflow(getattr(run, "workflow_id", None))
    except Exception:
        return False
