"""Syra's voice calls — runs that are an assistant talking to its own user, not a call.

Sysevo's assistant Syra speaks over this pipeline (spec:
docs/superpowers/specs/2026-09-23-syra-voice-over-dograh-design.md in the Sysevo repo).
Her workflow carries ``workflow_configurations.syra_voice = true``. A run of that workflow
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

from typing import Any, Optional


def is_syra_voice_config(workflow_configurations: Optional[dict[str, Any]]) -> bool:
    """True only for an explicit boolean ``syra_voice: true``."""
    return bool(workflow_configurations) and workflow_configurations.get("syra_voice") is True


async def is_syra_voice_workflow(workflow_id: Optional[int]) -> bool:
    """Looks the workflow up. Any failure answers False: a real call must never be
    exempted from billing because a lookup broke."""
    if not workflow_id:
        return False
    try:
        from api.db import db_client

        workflow = await db_client.get_workflow_by_id(workflow_id)
        return is_syra_voice_config(getattr(workflow, "workflow_configurations", None))
    except Exception:
        return False


async def is_syra_voice_run(workflow_run_id: int) -> bool:
    try:
        from api.db import db_client

        run = await db_client.get_workflow_run(workflow_run_id)
        return await is_syra_voice_workflow(getattr(run, "workflow_id", None))
    except Exception:
        return False
