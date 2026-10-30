from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace
from uuid import UUID

import pytest

from api.services.telephony.providers.twilio import floor_monitor as floor

PARENT = 'CA' + '1' * 32
MANAGER = 'CA' + '2' * 32
PREVIOUS = 'CA' + '3' * 32
ACTOR = '10000000-0000-0000-0000-000000000011'
REQUEST = '20000000-0000-0000-0000-000000000001'
ALLOWED = {'allowed': True, 'conference_name': 'call-' + PARENT, 'coach_call_sid': PARENT}


@pytest.fixture
def rpc(monkeypatch):
    mock = AsyncMock(return_value=ALLOWED)
    monkeypatch.setattr(floor, '_rpc', mock)
    return mock


async def test_claim_binds_client_request_and_verified_manager_leg(rpc):
    result = await floor.claim_floor_monitor(parent_call_sid=PARENT, manager_user_id=ACTOR,
        manager_call_sid=MANAGER, mode='whisper', session_id=REQUEST)
    assert result == ALLOWED
    rpc.assert_awaited_once_with('dialer_floor_monitor_join', {
        'p_session_id': REQUEST, 'p_parent_call_sid': PARENT, 'p_manager_user_id': ACTOR,
        'p_manager_call_sid': MANAGER, 'p_mode': 'whisper', 'p_legacy': False,
    })


async def test_old_sdk_replays_share_one_deterministic_legacy_identity(rpc):
    for _ in range(2):
        assert await floor.claim_floor_monitor(parent_call_sid=PARENT, manager_user_id=ACTOR,
            manager_call_sid=MANAGER, mode='listen') == ALLOWED
    first = rpc.await_args_list[0].args[1]
    second = rpc.await_args_list[1].args[1]
    assert first == second
    assert first['p_legacy'] is True
    UUID(first['p_session_id'])


@pytest.mark.parametrize('values', [
    {'session_id': 'invalid'}, {'manager_call_sid': PARENT}, {'manager_user_id': 'not-a-user'},
    {'parent_call_sid': 'CAshort'}, {'manager_call_sid': 'CAshort'}, {'mode': 'unknown'},
])
async def test_invalid_requests_do_not_touch_state(rpc, values):
    request = dict(parent_call_sid=PARENT, manager_user_id=ACTOR, manager_call_sid=MANAGER,
        mode='listen', session_id=REQUEST)
    request.update(values)
    assert await floor.claim_floor_monitor(**request) is None
    rpc.assert_not_awaited()


@pytest.mark.parametrize('result', [None, False, {}, {'allowed': False},
    {'allowed': True, 'conference_name': ''}, {'allowed': True, 'conference_name': 'valid', 'coach_call_sid': 'invalid'}])
async def test_denial_or_state_failure_never_opens_whisper(rpc, result):
    rpc.return_value = result
    assert await floor.claim_floor_monitor(parent_call_sid=PARENT, manager_user_id=ACTOR,
        manager_call_sid=MANAGER, mode='whisper', session_id=REQUEST) is None


async def test_mode_replacement_ends_only_the_verified_old_manager_leg(monkeypatch, rpc):
    rpc.side_effect = [
        {'allowed': False, 'previous_manager_call_sid': PREVIOUS}, True, ALLOWED,
    ]
    cancel = AsyncMock(return_value=True)
    monkeypatch.setattr(floor, 'cancel_call', cancel)
    assert await floor.claim_floor_monitor(parent_call_sid=PARENT, manager_user_id=ACTOR,
        manager_call_sid=MANAGER, mode='barge', session_id=REQUEST) == ALLOWED
    cancel.assert_awaited_once_with(call_sid=PREVIOUS)
    assert [call.args[0] for call in rpc.await_args_list] == [
        'dialer_floor_monitor_join', 'dialer_floor_monitor_left', 'dialer_floor_monitor_join',
    ]


@pytest.mark.parametrize('previous', [PARENT, MANAGER, 'invalid'])
async def test_replacement_never_ends_rep_or_current_manager(monkeypatch, rpc, previous):
    rpc.return_value = {'allowed': False, 'previous_manager_call_sid': previous}
    cancel = AsyncMock()
    monkeypatch.setattr(floor, 'cancel_call', cancel)
    assert await floor.claim_floor_monitor(parent_call_sid=PARENT, manager_user_id=ACTOR,
        manager_call_sid=MANAGER, mode='barge', session_id=REQUEST) is None
    cancel.assert_not_awaited()


async def test_failed_provider_end_does_not_join_second_mode(monkeypatch, rpc):
    rpc.return_value = {'allowed': False, 'previous_manager_call_sid': PREVIOUS}
    monkeypatch.setattr(floor, 'cancel_call', AsyncMock(return_value=False))
    assert await floor.claim_floor_monitor(parent_call_sid=PARENT, manager_user_id=ACTOR,
        manager_call_sid=MANAGER, mode='barge', session_id=REQUEST) is None
    assert rpc.await_count == 1


async def test_provider_leave_is_exact_and_distinguishes_unavailable_state(rpc):
    rpc.return_value = True
    assert await floor.close_floor_monitor_for_provider(MANAGER) is True
    rpc.assert_awaited_once_with('dialer_floor_monitor_left', {'p_manager_call_sid': MANAGER})
    rpc.return_value = False
    assert await floor.close_floor_monitor_for_provider(MANAGER) is False
    rpc.return_value = None
    assert await floor.close_floor_monitor_for_provider(MANAGER) is None


async def test_rpc_uses_server_authorization_and_current_contract(monkeypatch):
    monkeypatch.setattr(floor, 'SUPABASE_URL', 'https://example.invalid')
    monkeypatch.setattr(floor, 'SUPABASE_ANON_KEY', 'test-anon')
    monkeypatch.setattr(floor, 'SUPABASE_SERVICE_ROLE_KEY', 'test-service')
    response = MagicMock()
    response.json.return_value = ALLOWED
    client = AsyncMock()
    client.post.return_value = response
    client.__aenter__.return_value = client
    monkeypatch.setattr(floor.httpx, 'AsyncClient', MagicMock(return_value=client))
    assert await floor._rpc('dialer_floor_monitor_join', {'p_mode': 'listen'}) == ALLOWED
    assert client.post.await_args.args == ('https://example.invalid/rest/v1/rpc/dialer_floor_monitor_join',)
    assert client.post.await_args.kwargs['headers']['Authorization'] == 'Bearer test-service'
    assert client.post.await_args.kwargs['json'] == {'p_mode': 'listen'}


CONFERENCE = 'CF' + '4' * 32
PROSPECT = 'CA' + '5' * 32
MODE_TARGET = {**ALLOWED, 'conference_sid': CONFERENCE, 'manager_call_sid': MANAGER,
               'prospect_call_sid': PROSPECT}


@pytest.fixture
def carrier(monkeypatch):
    client = MagicMock()
    update = client.conferences.return_value.participants.return_value.update
    update.return_value = SimpleNamespace(muted=False, coaching=True, call_sid_to_coach=PARENT)
    monkeypatch.setattr(floor, '_twilio_client', AsyncMock(return_value=client))
    monkeypatch.setattr(floor, 'cancel_call', AsyncMock(return_value=True))
    return client


@pytest.mark.parametrize('mode,muted,coaching', [('listen', True, False), ('whisper', False, True), ('barge', False, False)])
async def test_mode_changes_update_only_the_existing_manager_participant(monkeypatch, rpc, carrier, mode, muted, coaching):
    rpc.side_effect = [MODE_TARGET, True]
    update = carrier.conferences.return_value.participants.return_value.update
    update.return_value = SimpleNamespace(muted=muted, coaching=coaching, call_sid_to_coach=PARENT)
    assert await floor.change_floor_monitor_mode(session_id=REQUEST, manager_user_id=ACTOR, mode=mode) == 'changed'
    carrier.conferences.assert_called_once_with(CONFERENCE)
    carrier.conferences.return_value.participants.assert_called_once_with(MANAGER)
    expected = {'muted': muted, 'coaching': coaching}
    if mode == 'whisper':
        expected['call_sid_to_coach'] = PARENT
    update.assert_called_once_with(**expected)
    carrier.calls.create.assert_not_called()
    floor.cancel_call.assert_not_awaited()
    assert [call.args[0] for call in rpc.await_args_list] == ['dialer_floor_monitor_mode_prepare', 'dialer_floor_monitor_mode_commit']
    assert rpc.await_args_list[0].args[1] == rpc.await_args_list[1].args[1]


async def test_inbound_mode_change_resolves_one_actual_conference(rpc, carrier):
    rpc.side_effect = [{**MODE_TARGET, 'conference_sid': None, 'conference_name': 'incoming-actual'}, True]
    carrier.conferences.list.return_value = [SimpleNamespace(sid=CONFERENCE)]
    assert await floor.change_floor_monitor_mode(session_id=REQUEST, manager_user_id=ACTOR, mode='whisper') == 'changed'
    carrier.conferences.list.assert_called_once_with(friendly_name='incoming-actual', status='in-progress', limit=2)
    carrier.conferences.assert_called_once_with(CONFERENCE)


@pytest.mark.parametrize('result,expected', [
    (None, 'unavailable'), ({'allowed': False}, 'unavailable'),
    ({'allowed': False, 'reason': 'busy'}, 'busy'), (floor._RPC_FAILED, 'failed'),
])
async def test_unavailable_or_busy_mode_state_never_touches_live_audio(rpc, carrier, result, expected):
    rpc.return_value = result
    assert await floor.change_floor_monitor_mode(session_id=REQUEST, manager_user_id=ACTOR, mode='barge') == expected
    carrier.conferences.assert_not_called()
    floor.cancel_call.assert_not_awaited()


@pytest.mark.parametrize('manager_sid', [PARENT, PROSPECT, 'invalid'])
async def test_mode_change_never_modifies_or_ends_rep_or_prospect(rpc, carrier, manager_sid):
    rpc.side_effect = [{**MODE_TARGET, 'manager_call_sid': manager_sid}, None]
    assert await floor.change_floor_monitor_mode(session_id=REQUEST, manager_user_id=ACTOR, mode='barge') == 'failed'
    carrier.conferences.assert_not_called()
    floor.cancel_call.assert_not_awaited()
    assert rpc.await_args_list[-1].args[0] == 'dialer_floor_monitor_mode_release'


@pytest.mark.parametrize('failure', ['provider', 'response', 'commit', 'missing_conference', 'ambiguous_conference'])
async def test_unconfirmed_mode_ends_only_verified_manager_and_releases_lease(rpc, carrier, failure):
    target = dict(MODE_TARGET)
    commit_result = False if failure == 'commit' else True
    if failure.endswith('conference'):
        target['conference_sid'] = None
        carrier.conferences.list.return_value = [] if failure == 'missing_conference' else [SimpleNamespace(sid=CONFERENCE)] * 2
    rpc.side_effect = [target, commit_result, None] if failure == 'commit' else [target, None]
    update = carrier.conferences.return_value.participants.return_value.update
    if failure == 'provider':
        update.side_effect = RuntimeError('Provider unavailable')
    elif failure == 'response':
        update.return_value = SimpleNamespace(muted=True, coaching=False, call_sid_to_coach=None)
    assert await floor.change_floor_monitor_mode(session_id=REQUEST, manager_user_id=ACTOR, mode='whisper') == 'failed'
    floor.cancel_call.assert_awaited_once_with(call_sid=MANAGER)
    name, values = rpc.await_args_list[-1].args
    assert name == 'dialer_floor_monitor_mode_release'
    assert values == {key: value for key, value in rpc.await_args_list[0].args[1].items() if key != 'p_mode'}


@pytest.mark.parametrize('values', [{'session_id': 'bad'}, {'manager_user_id': 'bad'}, {'mode': 'bad'}])
async def test_invalid_mode_request_never_touches_state(rpc, carrier, values):
    request = dict(session_id=REQUEST, manager_user_id=ACTOR, mode='listen')
    request.update(values)
    assert await floor.change_floor_monitor_mode(**request) == 'unavailable'
    rpc.assert_not_awaited()
    carrier.conferences.assert_not_called()
