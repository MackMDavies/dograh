"""Which AI agent takes the inbound calls nobody answers. Free of I/O so it can be tested.

The dialer's Agents page (dialer_agent_assignments, role 'inbound') is where this is chosen.
Before it was wired, three server settings decided it -- SYSEVO_SAM_INBOUND_NUMBER,
SYSEVO_SAM_INBOUND_WORKFLOW_ID and SYSEVO_SAM_RING_FIRST_SECONDS -- and nobody could see
them: the number setting pointed at a dead line for a day and every rung-out caller was
dropped. So:

- The agent picked in the app wins, and it answers on the number assigned to ITS OWN
  workflow. There is no number to type, so there is no number to get wrong.
- Nothing picked in the app keeps the server settings, so turning this on changes nothing
  until somebody chooses an agent.
- A picked agent that cannot answer (no number, workflow gone, retired from the catalogue)
  is reported as not live. It does not quietly fall back to the server settings: the page
  would show one agent while callers reached another.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from api.services.telephony.dialer.sam_handoff_rules import ring_first_seconds

_E164 = re.compile(r"^\+\d{7,15}$")


@dataclass(frozen=True)
class InboundAgent:
    workflow_id: int | None
    #: E.164 the agent answers on; "" means no agent can take the call.
    number: str
    #: Seconds reps' lines ring before the caller is handed over; 0 = no timer.
    ring_first_seconds: int
    #: 'app' (the Agents page), 'server' (the old settings) or 'off'.
    source: str
    #: Why callers are not reaching an agent, in words for the page. "" when live.
    problem: str


def _e164(value: Any) -> str:
    text = str(value or "").strip()
    return text if _E164.match(text) else ""


def choose_inbound_agent(
    *,
    assignment: dict | None,
    workflow_id: int | None,
    numbers: list[str],
    env_number: str | None,
    env_workflow_id: str | None,
    env_ring_first: str | None,
) -> InboundAgent:
    """assignment: the 'inbound' row with its demo_agent embedded. workflow_id: that agent's
    Dograh workflow, if it still exists. numbers: E.164s assigned to it, newest first."""
    picked = (assignment or {}).get("demo_agent") if (assignment or {}).get("is_active", True) else None
    if picked:
        ring = ring_first_seconds(str((assignment or {}).get("ring_first_seconds") or ""))
        if not picked.get("is_active", True):
            return InboundAgent(workflow_id, "", ring, "app", "The chosen agent has been retired from the catalogue.")
        if workflow_id is None:
            return InboundAgent(None, "", ring, "app", "The chosen agent's Dograh workflow no longer exists.")
        number = next((n for n in (_e164(x) for x in numbers) if n), "")
        if not number:
            return InboundAgent(workflow_id, "", ring, "app", "The chosen agent has no phone number assigned to it.")
        return InboundAgent(workflow_id, number, ring, "app", "")

    number = _e164(env_number)
    try:
        env_workflow = int(str(env_workflow_id or "").strip())
    except ValueError:
        env_workflow = None
    if number:
        return InboundAgent(env_workflow, number, ring_first_seconds(env_ring_first), "server", "")
    return InboundAgent(env_workflow, "", 0, "off", "No inbound agent is chosen.")


def hands_to_agent(rows: list[dict]) -> bool:
    """Whether a rep's number hands unanswered callers to the agent. Unknown numbers and a
    failed read keep today's behaviour (they do): the alternative is "nobody available"."""
    return not any(row.get("hands_to_agent") is False for row in rows or [])
