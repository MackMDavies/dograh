"""The inbound routes follow the agent chosen on the dialer's Agents page, per rep number."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services.telephony.dialer.inbound_agent_rules import InboundAgent

_MODULE = "api.services.telephony.dialer.signalwire_routes"
_SECRET = "shhh"
_LIVE = InboundAgent(workflow_id=214, number="+12393231195", ring_first_seconds=20, source="app", problem="")
_OFF = InboundAgent(workflow_id=None, number="", ring_first_seconds=0, source="off", problem="No inbound agent is chosen.")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SIGNALWIRE_WEBHOOK_KEY", _SECRET)


def _authed(body, query=None) -> MagicMock:
    request = MagicMock()
    request.query_params = {"k": _SECRET, **(query or {})}
    request.headers = {"content-type": "application/json"}
    request.body = AsyncMock(return_value=json.dumps(body).encode())
    request.json = AsyncMock(return_value=body)
    request.url = MagicMock(path="/x")
    return request


def _steps(response) -> list:
    return json.loads(response.body)["sections"]["main"]


def _connects_to(steps) -> list[str]:
    return [json.dumps(s) for s in steps if "connect" in json.dumps(s)]


_CALL = {"call": {"call_id": "abc", "from": "+447700900000", "to": "+12092669253"}}


async def _inbound(*, agent, hands=True, plan=()):
    from api.services.telephony.dialer.signalwire_routes import handle_sw_inbound

    spawned = []
    with (
        patch(f"{_MODULE}.resolve_inbound_plan", new=AsyncMock(return_value=list(plan))),
        patch(f"{_MODULE}.create_inbound_call", new=AsyncMock(return_value=None)),
        patch(f"{_MODULE}.get_backend_endpoints", new=AsyncMock(return_value=("https://api.example", ""))),
        patch(f"{_MODULE}.current_inbound_agent", new=AsyncMock(return_value=agent)),
        patch(f"{_MODULE}.number_hands_to_agent", new=AsyncMock(return_value=hands)),
        patch(f"{_MODULE}.sam_handoff.spawn", new=lambda coro: (spawned.append(coro), coro.close())),
    ):
        response = await handle_sw_inbound(_authed(_CALL))
    return _steps(response), spawned


async def test_nobody_available_goes_to_the_chosen_agents_own_number():
    steps, _ = await _inbound(agent=_LIVE)
    assert any("+12393231195" in s for s in _connects_to(steps))


async def test_a_number_switched_off_for_hand_over_tells_the_caller_nobody_is_available():
    steps, _ = await _inbound(agent=_LIVE, hands=False)
    assert not _connects_to(steps)
    assert steps[-1] == {"hangup": {}}


async def test_no_agent_chosen_anywhere_keeps_the_nobody_available_message():
    steps, _ = await _inbound(agent=_OFF)
    assert not _connects_to(steps)


_REPS = [{"user_id": "user-a", "reach": "live", "forward_number": None}]


async def test_reps_ring_first_then_the_hand_over_is_scheduled():
    _, spawned = await _inbound(agent=_LIVE, plan=_REPS)
    assert len(spawned) == 1


async def test_no_hand_over_timer_when_the_number_is_switched_off():
    _, spawned = await _inbound(agent=_LIVE, hands=False, plan=_REPS)
    assert spawned == []


async def test_no_hand_over_timer_when_ring_first_is_not_set():
    agent = InboundAgent(workflow_id=214, number="+12393231195", ring_first_seconds=0, source="app", problem="")
    _, spawned = await _inbound(agent=agent, plan=_REPS)
    assert spawned == []


async def test_a_rerouted_caller_goes_to_the_chosen_agent():
    from api.services.telephony.dialer.signalwire_routes import handle_sw_inbound_reroute

    with patch(f"{_MODULE}.current_inbound_agent", new=AsyncMock(return_value=_LIVE)):
        response = await handle_sw_inbound_reroute(_authed({}, {"call_id": "abc", "mode": "agent"}))
    assert any("+12393231195" in s for s in _connects_to(_steps(response)))


async def test_the_status_check_reports_what_the_server_resolved():
    from api.services.telephony.dialer.signalwire_routes import handle_inbound_agent_status

    with patch(f"{_MODULE}.current_inbound_agent", new=AsyncMock(return_value=_LIVE)) as resolve:
        response = await handle_inbound_agent_status(user=MagicMock())
    body = json.loads(response.body)
    assert body == {"live": True, "source": "app", "number": "+12393231195", "workflow_id": 214, "ring_first_seconds": 20, "problem": ""}
    resolve.assert_awaited_with(fresh=True)
