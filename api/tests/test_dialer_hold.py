"""Actual Hold orchestration; SQL/provider boundaries are mocked."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree

import pytest

from api.services.telephony.providers.twilio import dialer_hold as hold

MOD = "api.services.telephony.providers.twilio.dialer_hold"
ARGS = dict(parent_call_sid="CA"+"1"*32,child_call_sid="CA"+"2"*32,leg_id="00000000-0000-0000-0000-000000000001",account_sid="AC"+"3"*32)


@pytest.fixture
def deps():
    rpc = AsyncMock(return_value={"status":"waiting"})
    with (
        patch(f"{MOD}.dialer_agent_handoff._rpc",rpc),
        patch(f"{MOD}.get_backend_endpoints",new=AsyncMock(return_value=("https://api.test","wss://api.test"))),
        patch(f"{MOD}.dialer_agent_handoff.prepare_agent_handoff",new=AsyncMock(return_value=SimpleNamespace(twiml="agent-stream"))) as prepare,
        patch(f"{MOD}.notify_inbound_call") as notify,
    ):
        yield SimpleNamespace(rpc=rpc,prepare=prepare,notify=notify)


async def test_hold_starts_with_a_message_and_signed_callback_target(deps):
    root = ElementTree.fromstring(await hold.hold_twiml(**ARGS,begin=True))
    says = [say.text for say in root.findall("Say")]
    # 24 Oct: no automatic recording notice on outbound; the rep says it.
    assert says == ["Thanks for picking up, connecting you now."]
    assert root.find("Pause").attrib["length"] == "1"
    redirect = root.find("Redirect")
    assert redirect.attrib["method"] == "POST"
    assert "/dialer-parallel-hold-next?parent_call_sid=" in redirect.text
    assert root.find("Dial") is None and root.find("Connect") is None
    assert deps.rpc.await_args.args[1]["p_begin"] is True


async def test_wait_poll_does_not_repeat_the_intro_or_originate_another_call(deps):
    root = ElementTree.fromstring(await hold.hold_twiml(**ARGS))
    assert root.find("Say") is None
    assert root.find("Redirect") is not None
    deps.prepare.assert_not_awaited()


async def test_available_original_rep_uses_existing_invitation_and_conference(deps):
    deps.rpc.return_value = {"status":"offered","inbound_call_id":"offer","conference_name":"call-"+ARGS["child_call_sid"]}
    root = ElementTree.fromstring(await hold.hold_twiml(**ARGS))
    conference = root.find("Dial/Conference")
    assert conference.text == "call-"+ARGS["child_call_sid"]
    assert conference.attrib["startConferenceOnEnter"] == "false"
    assert conference.attrib["record"] == "record-from-start"
    deps.notify.assert_called_once_with("offer")
    deps.prepare.assert_not_awaited()


async def test_replayed_offer_does_not_push_a_second_notification(deps):
    deps.rpc.return_value = {"status":"offered","inbound_call_id":None,"conference_name":"call-"+ARGS["child_call_sid"]}
    await hold.hold_twiml(**ARGS)
    deps.notify.assert_not_called()


async def test_busy_rep_at_deadline_hands_same_answered_call_to_agent(deps):
    deps.rpc.return_value = {"status":"agent"}
    assert await hold.hold_twiml(**ARGS) == "agent-stream"
    deps.prepare.assert_awaited_once_with(**ARGS,allow_fallback=True)


async def test_completed_held_call_is_not_reopened(deps):
    deps.rpc.return_value = {"status":"completed"}
    assert "<Hangup/>" in await hold.hold_twiml(**ARGS)
    deps.prepare.assert_not_awaited()


async def test_unrelated_call_cannot_enter_hold(deps):
    deps.rpc.return_value = None
    assert await hold.hold_twiml(**ARGS,begin=True) is None
    deps.prepare.assert_not_awaited()


async def test_agent_unavailability_ends_the_held_call(deps):
    deps.rpc.return_value = {"status":"agent"}
    deps.prepare.side_effect = hold.dialer_agent_handoff.AgentHandoffUnavailable("No configured agent")
    assert "<Hangup/>" in await hold.hold_twiml(**ARGS)


async def test_transient_storage_failure_is_retryable(deps):
    deps.rpc.return_value = {"status":"agent"}
    deps.prepare.side_effect = hold.dialer_agent_handoff.AgentHandoffUnavailable("Storage unavailable",retryable=True)
    with pytest.raises(hold.dialer_agent_handoff.AgentHandoffUnavailable):
        await hold.hold_twiml(**ARGS)


async def test_wrong_conference_never_notifies_or_bridges(deps):
    deps.rpc.return_value = {"status":"offered","inbound_call_id":"offer","conference_name":"unrelated"}
    with pytest.raises(hold.dialer_agent_handoff.AgentHandoffUnavailable):
        await hold.hold_twiml(**ARGS)
    deps.notify.assert_not_called()
