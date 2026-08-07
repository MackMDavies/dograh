"""Sysevo caller memory pre-call fetch.

Fires at call start for any telephony call that has a caller_number in context.
Calls the Sysevo dograh-memory-inbound-hook edge function, which returns
dynamic_variables (caller_name, caller_memory block, etc.) to be merged into
the Pipecat engine's call context vars.

These variables become available as {{caller_name}}, {{caller_memory}}, etc. in
the agent's system prompt template.
"""

import os
from typing import Any

import httpx
from loguru import logger

# Timeout is intentionally shorter than the ringer timeout so we never block a
# call for too long. The event_handlers ringer plays while we wait.
_MEMORY_FETCH_TIMEOUT = 8.0


def lead_number_for_call(
    ctx: dict[str, Any], call_type: str | None
) -> str:
    """The OTHER party's number — the one caller memory is filed under.

    On an inbound call the lead is `caller_number`. On an OUTBOUND call
    `caller_number` is our own outbound caller ID: at the time of writing
    +16592127650 appears on 90 runs covering 60 different leads. Looking memory
    up by it finds nothing, because the write path files memory under the lead's
    number — so every redial opened cold on someone we had already spoken to.

    Note the outbound branch deliberately has NO `caller_number` fallback. It is
    better to return "" and skip memory than to look a caller up by a number
    shared across every lead in the campaign: if a record ever gets filed under
    it, EVERY outbound call loads that one stranger's memory and the agent
    discusses a conversation it had with somebody else. Silence beats confident
    fabrication.

    Mirrors leadNumberForCall in supabase/functions/_shared/memory/campaign-identity.ts,
    which the post-call write path already uses. Read and write must agree on the
    key or memory is written where it is never looked for.
    """
    outbound = str(call_type or "").strip().lower() == "outbound"
    keys = ("called_number", "phone_number") if outbound else (
        "caller_number",
        "called_number",
        "phone_number",
    )
    for key in keys:
        value = ctx.get(key)
        if not isinstance(value, str):
            continue
        value = value.strip()
        # Unrendered CSV placeholders ("[your phone number]") reach us as
        # literal text and must never be treated as identity.
        if not value or value[0] in "[{<":
            continue
        return value
    return ""


async def execute_memory_pre_call_fetch(
    *,
    url: str,
    secret: str,
    call_context_vars: dict[str, Any],
    workflow_id: int,
    organization_id: int | None = None,
    call_type: str | None = None,
    workflow_run_id: int | None = None,
) -> dict[str, Any]:
    """POST caller context to the Sysevo memory hook and return dynamic_variables.

    Returns an empty dict on any error so the call always proceeds.
    """
    lead_number = lead_number_for_call(call_context_vars, call_type)
    our_number = (
        call_context_vars.get("caller_number", "")
        if str(call_type or "").strip().lower() == "outbound"
        else call_context_vars.get("called_number", "")
    )
    payload = {
        "event": "call_inbound",
        # from_number is what the hook looks caller memory up by, so it must be
        # the LEAD on both directions, not literally whoever placed the call.
        "call_inbound": {
            "agent_id": workflow_id,
            "from_number": lead_number,
            "to_number": our_number,
        },
    }
    if not lead_number:
        logger.info(
            "[memory-pre-call] no lead number resolvable "
            f"(call_type={call_type!r}) — skipping memory lookup"
        )
        return await _augment_with_prior_contact(
            {}, call_context_vars, organization_id, call_type, workflow_run_id
        )
    headers: dict[str, str] = {
        "Content-Type": "application/json",
    }
    if secret:
        headers["X-Sysevo-Secret"] = secret

    try:
        async with httpx.AsyncClient(timeout=_MEMORY_FETCH_TIMEOUT) as client:
            response = await client.post(url, json=payload, headers=headers)

        if not response.is_success:
            logger.warning(
                f"[memory-pre-call] HTTP {response.status_code} from memory hook"
            )
            return {}

        data: dict = response.json()

        # Extract dynamic_variables from Dograh response envelope
        call_inbound = data.get("call_inbound", {})
        if isinstance(call_inbound, dict):
            dynamic_vars = call_inbound.get("dynamic_variables", {})
        else:
            dynamic_vars = data.get("dynamic_variables", {})

        if not isinstance(dynamic_vars, dict):
            return {}

        caller_name = dynamic_vars.get("caller_name", "")
        caller_known = dynamic_vars.get("caller_known", "false")
        logger.info(
            f"[memory-pre-call] caller_known={caller_known} name={caller_name!r}"
        )
        return await _augment_with_prior_contact(
            dynamic_vars, call_context_vars, organization_id, call_type, workflow_run_id
        )

    except httpx.TimeoutException:
        logger.warning("[memory-pre-call] Timed out — proceeding without memory")
        return await _augment_with_prior_contact(
            {}, call_context_vars, organization_id, call_type, workflow_run_id
        )
    except Exception as e:
        logger.error(f"[memory-pre-call] Unexpected error: {e}")
        return await _augment_with_prior_contact(
            {}, call_context_vars, organization_id, call_type, workflow_run_id
        )


async def _augment_with_prior_contact(
    dynamic_vars: dict[str, Any],
    call_context_vars: dict[str, Any],
    organization_id: int | None,
    call_type: str | None = None,
    workflow_run_id: int | None = None,
) -> dict[str, Any]:
    """Fill identity gaps from a previous OUTBOUND attempt to this number.

    Three cases an inbound call can be, and the agent must handle all three:

      1. We have SPOKEN to them  -> Sysevo caller memory answers. Richest source,
         so it always wins; nothing here overwrites it.
      2. We DIALLED them and never connected -> memory knows nothing, but the
         campaign list does. They are almost certainly ringing back. This is the
         gap this function closes, and on a cold-calling operation it is the
         majority case.
      3. Neither -> a genuine stranger. Everything stays empty and the agent
         opens cold, which is correct.

    `caller_known_from` tells the prompt which of the three it is, so the greeting
    can differ ("thanks for calling back" vs "how can I help?") without the model
    having to infer it.
    """
    out = dict(dynamic_vars or {})
    already_known = str(out.get("caller_known", "")).lower() == "true" or bool(
        str(out.get("caller_name", "")).strip()
    )

    prior: dict[str, Any] = {}
    try:
        from api.services.pipecat.prior_contact import lookup_prior_outbound_contact

        prior = await lookup_prior_outbound_contact(
            lead_number_for_call(call_context_vars, call_type),
            organization_id=organization_id,
            exclude_run_id=workflow_run_id,
        )
    except Exception as e:  # noqa: BLE001 — recognition must never block a call
        logger.warning(f"[memory-pre-call] prior-contact lookup failed: {e}")

    if prior:
        # Fill blanks only. A name from a real conversation beats a name from a
        # spreadsheet — the person who answers is not always the person listed.
        if not str(out.get("caller_name", "")).strip() and prior.get("full_name"):
            out["caller_name"] = prior["full_name"]
        if not str(out.get("caller_company", "")).strip() and prior.get("company"):
            out["caller_company"] = prior["company"]
        if not str(out.get("caller_email", "")).strip() and prior.get("email"):
            out["caller_email"] = prior["email"]
        out.setdefault("prior_outbound_attempts", str(prior.get("prior_attempts", 0)))

    if already_known:
        out["caller_known_from"] = "memory"
    elif prior:
        out["caller_known_from"] = "campaign"
        # caller_known stays FALSE on purpose. Sam's prompt defines it as "we have
        # spoken to this number before" and instructs the agent to "refer to what
        # was actually said last time". For a campaign-only match there IS no last
        # time — only a dial that was never answered — so setting it true invites
        # the agent to invent a conversation that never happened. `is_callback`
        # carries this case instead, and caller_name/caller_company are still
        # populated so the agent can greet them correctly.
        out["is_callback"] = "true"
    else:
        out.setdefault("caller_known_from", "none")
        out.setdefault("caller_known", "false")

    # Every variable the prompt can reference must exist as a STRING. A missing
    # key or a None renders as "None" in the greeting — "Hi, is that None?" is
    # worse than no greeting at all.
    for key in ("caller_name", "caller_company", "caller_email", "prior_outbound_attempts"):
        if not out.get(key):
            out[key] = ""
    out.setdefault("is_callback", "false")

    logger.info(
        f"[memory-pre-call] resolved caller_known_from={out['caller_known_from']} "
        f"name={out.get('caller_name', '')!r} company={out.get('caller_company', '')!r}"
    )
    return out
