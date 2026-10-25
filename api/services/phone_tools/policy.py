"""Rules for the built-in phone tools (Voice AI -> Tools -> Phone tools).

Each organization decides, per tool, whether its agents may use it: always ("on"),
only in the situation the organization describes ("rule"), or never ("off"). Nothing
saved means every tool is on, so the tools work with nothing to set up.

A rule is plain words for the model ("only when calling a company's main number"); it
is appended to the tool's description, which is where the model decides whether a
tool applies.

Loading the rules must never break a call: any failure falls back to the defaults.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger

from api.db import db_client
from api.enums import OrganizationConfigurationKey

# keypad: the agent presses keys on the call (switchboards, phone menus).
# caller_keypad: the agent hears the keys the caller presses.
# sms: the agent texts the caller during the call (builtin.py).
# recording: the agent pauses/resumes the recording when the caller asks; with
#   start_paused, every call starts unrecorded until the caller agrees.
# hold: music while a lookup tool (HTTP / MCP) takes more than a moment.
# payments: text the caller one of the business's own payment links (builtin.py).
PHONE_TOOL_IDS = ("keypad", "caller_keypad", "sms", "recording", "hold", "payments")

MODES = ("on", "rule", "off")
MAX_RULE_CHARS = 300


@dataclass(frozen=True)
class ToolRule:
    mode: str = "on"
    rule: str = ""
    start_paused: bool = False  # recording only


class PhoneToolsPolicy:
    def __init__(self, tools: dict[str, ToolRule]):
        self._tools = {tool_id: tools.get(tool_id, ToolRule()) for tool_id in PHONE_TOOL_IDS}

    def enabled(self, tool_id: str) -> bool:
        return self._tools.get(tool_id, ToolRule()).mode != "off"

    def rule(self, tool_id: str) -> Optional[str]:
        entry = self._tools.get(tool_id, ToolRule())
        return entry.rule if entry.mode == "rule" and entry.rule else None

    def recording_starts_paused(self) -> bool:
        return self.enabled("recording") and self._tools["recording"].start_paused

    def describe(self, tool_id: str, description: str) -> str:
        rule = self.rule(tool_id)
        if not rule:
            return description
        return f"{description} Your organization only allows this tool when: {rule}"

    def to_dict(self) -> dict[str, Any]:
        tools: dict[str, Any] = {}
        for tool_id, entry in self._tools.items():
            tools[tool_id] = {"mode": entry.mode, "rule": entry.rule}
            if tool_id == "recording":
                tools[tool_id]["start_paused"] = entry.start_paused
        return {"tools": tools}


def _clean_rule(value: Any) -> str:
    return " ".join(str(value or "").split())[:MAX_RULE_CHARS]


def normalize_policy(raw: Any) -> PhoneToolsPolicy:
    tools_raw = raw.get("tools") if isinstance(raw, dict) else None
    if not isinstance(tools_raw, dict):
        tools_raw = {}
    tools: dict[str, ToolRule] = {}
    for tool_id in PHONE_TOOL_IDS:
        entry = tools_raw.get(tool_id)
        if not isinstance(entry, dict):
            continue
        mode = entry.get("mode")
        if mode not in MODES:
            continue
        rule = _clean_rule(entry.get("rule"))
        if mode == "rule" and not rule:
            mode = "on"
        tools[tool_id] = ToolRule(
            mode=mode,
            rule=rule if mode == "rule" else "",
            start_paused=tool_id == "recording" and entry.get("start_paused") is True,
        )
    return PhoneToolsPolicy(tools)


async def load_policy(organization_id: Optional[int]) -> PhoneToolsPolicy:
    if not organization_id:
        return normalize_policy(None)
    try:
        row = await db_client.get_configuration(
            organization_id, OrganizationConfigurationKey.PHONE_TOOLS_POLICY.value
        )
        return normalize_policy(row.value if row else None)
    except Exception as exc:  # noqa: BLE001 -- the call goes ahead on the defaults
        logger.warning(f"phone tools policy unavailable for org {organization_id}: {exc}")
        return normalize_policy(None)


async def save_policy(organization_id: int, raw: Any) -> PhoneToolsPolicy:
    policy = normalize_policy(raw)
    await db_client.upsert_configuration(
        organization_id,
        OrganizationConfigurationKey.PHONE_TOOLS_POLICY.value,
        policy.to_dict(),
    )
    return policy
