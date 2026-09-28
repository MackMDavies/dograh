"""Which agent takes inbound calls nobody answers, decided from the dialer's Agents page."""
from api.services.telephony.dialer.inbound_agent_rules import (
    InboundAgent,
    choose_inbound_agent,
    hands_to_agent,
)

ENV = dict(env_number="+12393231195", env_workflow_id="214", env_ring_first="20")
PICKED = {"is_active": True, "ring_first_seconds": 25, "demo_agent": {"workflow_uuid": "wf-uuid", "is_active": True}}


def _choose(**over):
    args = dict(assignment=PICKED, workflow_id=214, numbers=["+12393231195"], **ENV)
    args.update(over)
    return choose_inbound_agent(**args)


def test_the_agent_picked_in_the_app_wins_and_answers_on_its_own_number():
    agent = _choose(numbers=["+15550001111"], env_number="+12392190585")
    assert agent == InboundAgent(workflow_id=214, number="+15550001111", ring_first_seconds=25, source="app", problem="")


def test_nothing_picked_in_the_app_keeps_todays_server_settings():
    for unset in (None, {}, {"is_active": True, "demo_agent": None, "ring_first_seconds": None}):
        agent = _choose(assignment=unset, workflow_id=None, numbers=[])
        assert agent == InboundAgent(workflow_id=214, number="+12393231195", ring_first_seconds=20, source="server", problem="")


def test_nothing_anywhere_is_off_and_says_so():
    agent = _choose(assignment=None, workflow_id=None, numbers=[], env_number="", env_workflow_id="", env_ring_first="")
    assert agent.source == "off"
    assert agent.number == ""
    assert agent.problem


def test_a_picked_agent_with_no_phone_number_is_not_live_and_does_not_fall_back_silently():
    agent = _choose(numbers=[])
    assert agent.source == "app"
    assert agent.number == ""
    assert "number" in agent.problem


def test_a_picked_agent_whose_workflow_is_gone_is_not_live():
    agent = _choose(workflow_id=None)
    assert agent.number == ""
    assert "workflow" in agent.problem


def test_a_retired_catalogue_entry_is_not_live():
    agent = _choose(assignment={**PICKED, "demo_agent": {"workflow_uuid": "wf-uuid", "is_active": False}})
    assert agent.number == ""
    assert agent.problem


def test_only_real_e164_numbers_are_used_and_the_newest_wins():
    # numbers arrive newest first; junk is skipped rather than dialled
    agent = _choose(numbers=["not-a-number", "+15550002222", "+15550001111"])
    assert agent.number == "+15550002222"


def test_ring_first_is_bounded_and_blank_means_no_hand_over_timer():
    assert _choose(assignment={**PICKED, "ring_first_seconds": None}).ring_first_seconds == 0
    assert _choose(assignment={**PICKED, "ring_first_seconds": 2}).ring_first_seconds == 0
    assert _choose(assignment={**PICKED, "ring_first_seconds": 600}).ring_first_seconds == 0
    assert _choose(assignment={**PICKED, "ring_first_seconds": 45}).ring_first_seconds == 45


def test_a_rep_number_hands_over_unless_it_was_switched_off():
    assert hands_to_agent([]) is True                          # unknown number: today's behaviour
    assert hands_to_agent([{"hands_to_agent": True}]) is True
    assert hands_to_agent([{"hands_to_agent": None}]) is True
    assert hands_to_agent([{"hands_to_agent": False}]) is False
