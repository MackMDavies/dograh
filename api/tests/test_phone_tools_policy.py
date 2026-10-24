"""Phone tools policy: the per-organization rules for the built-in phone tools."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.services.phone_tools import policy as phone_tools


def test_nothing_saved_means_every_built_in_tool_is_on():
    policy = phone_tools.normalize_policy(None)
    assert policy.enabled("keypad")
    assert policy.enabled("caller_keypad")
    assert policy.rule("keypad") is None


def test_a_tool_can_be_switched_off():
    policy = phone_tools.normalize_policy({"tools": {"keypad": {"mode": "off"}}})
    assert not policy.enabled("keypad")
    assert policy.enabled("caller_keypad")


def test_a_rule_limits_when_the_tool_is_used():
    policy = phone_tools.normalize_policy(
        {"tools": {"keypad": {"mode": "rule", "rule": "  only on   outbound calls "}}}
    )
    assert policy.enabled("keypad")
    assert policy.rule("keypad") == "only on outbound calls"
    described = policy.describe("keypad", "Press digits.")
    assert described.startswith("Press digits.")
    assert "only on outbound calls" in described


def test_a_rule_with_no_words_is_the_same_as_on():
    policy = phone_tools.normalize_policy({"tools": {"keypad": {"mode": "rule", "rule": "   "}}})
    assert policy.enabled("keypad")
    assert policy.rule("keypad") is None
    assert policy.describe("keypad", "Press digits.") == "Press digits."


def test_rules_are_capped_and_garbage_falls_back_to_defaults():
    policy = phone_tools.normalize_policy(
        {"tools": {
            "keypad": {"mode": "rule", "rule": "x" * 5000},
            "caller_keypad": {"mode": "sideways"},
            "not_a_tool": {"mode": "off"},
        }}
    )
    assert len(policy.rule("keypad")) == phone_tools.MAX_RULE_CHARS
    assert policy.enabled("caller_keypad")
    assert "not_a_tool" not in policy.to_dict()["tools"]
    assert phone_tools.normalize_policy("nonsense").enabled("keypad")
    assert phone_tools.normalize_policy({"tools": []}).enabled("keypad")


def test_round_trip_lists_every_tool():
    saved = phone_tools.normalize_policy(
        {"tools": {"caller_keypad": {"mode": "rule", "rule": "to confirm"}}}
    ).to_dict()
    assert set(saved["tools"]) == set(phone_tools.PHONE_TOOL_IDS)
    assert saved["tools"]["caller_keypad"] == {"mode": "rule", "rule": "to confirm"}
    assert saved["tools"]["keypad"] == {"mode": "on", "rule": ""}
    assert phone_tools.normalize_policy(saved).to_dict() == saved


@pytest.mark.asyncio
async def test_load_reads_the_organization_setting():
    row = SimpleNamespace(value={"tools": {"keypad": {"mode": "off"}}})
    with patch.object(phone_tools.db_client, "get_configuration", AsyncMock(return_value=row)) as get:
        policy = await phone_tools.load_policy(7)
    assert not policy.enabled("keypad")
    assert get.await_args.args == (7, "PHONE_TOOLS_POLICY")


@pytest.mark.asyncio
async def test_load_never_breaks_a_call():
    with patch.object(
        phone_tools.db_client, "get_configuration", AsyncMock(side_effect=RuntimeError("db down"))
    ):
        policy = await phone_tools.load_policy(7)
    assert policy.enabled("keypad") and policy.enabled("caller_keypad")
    assert (await phone_tools.load_policy(None)).enabled("keypad")
