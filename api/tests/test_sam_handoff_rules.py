"""Sam INBOUND hand-off rules: ring first, then Sam; Sam hands a caller back to the rep."""
from api.services.telephony.dialer.sam_handoff_rules import (
    calling_transfer_body,
    phone_tail,
    ring_first_seconds,
    transfer_request_allowed,
)
from api.services.telephony.dialer.swml import build_agent_overflow_swml

SAM = 214


def test_ring_first_is_off_unless_set_to_a_sane_value():
    assert ring_first_seconds(None) == 0
    assert ring_first_seconds("") == 0
    assert ring_first_seconds("abc") == 0
    assert ring_first_seconds("2") == 0      # would bounce before the dialer rings once
    assert ring_first_seconds("600") == 0    # would hang a caller on hold for ten minutes
    assert ring_first_seconds("20") == 20


def test_transfer_body_moves_the_call_to_our_own_swml():
    body = calling_transfer_body("call-123", "https://api.example/api/v1/telephony/sw-inbound-reroute?call_id=call-123&mode=agent")
    assert body == {
        "command": "calling.transfer",
        "id": "call-123",
        "params": {"dest": "https://api.example/api/v1/telephony/sw-inbound-reroute?call_id=call-123&mode=agent"},
    }


def _allowed(**over):
    args = dict(run_workflow_id=SAM, run_is_completed=False, run_caller_number="+17727736378",
                claimed_caller_number="+1 (772) 773-6378", sam_workflow_id=SAM)
    args.update(over)
    return transfer_request_allowed(**args)


def test_a_live_sam_call_for_that_caller_may_transfer():
    assert _allowed() == (True, "")


def test_a_forged_or_stale_request_is_refused():
    assert _allowed(run_workflow_id=None)[0] is False
    assert _allowed(run_workflow_id=200)[0] is False          # Sam Outbound, not inbound
    assert _allowed(run_is_completed=True)[0] is False
    assert _allowed(claimed_caller_number="+15550000000")[0] is False
    assert _allowed(claimed_caller_number="")[0] is False


def test_phone_tail_ignores_formatting():
    assert phone_tail("+1 (772) 773-6378") == "7727736378"


def test_back_to_sam_says_why_first():
    doc = build_agent_overflow_swml(agent_number="+12392190585", prelude="They're tied up right now.")
    steps = doc["sections"]["main"]
    assert steps[0] == {"play": {"url": "say:They're tied up right now."}}
    assert any("connect" in s for s in steps)
