"""Call-free tests of the actual pre-dial client and new request validation.

Run with --noconftest: these tests need no production/test database or runtime
voice pipeline. The route's Pydantic request classes are compiled from their
actual AST to avoid importing its unrelated provider/database dependency graph.
"""
import ast
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import UUID
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, Mock, MagicMock, patch
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from api.services.dial_permission_check import check_dial_permitted

HANDOFF = '11111111-1111-4111-8111-111111111111'


def request_model():
    source = Path(__file__).resolve().parents[2] / 'routes/campaign.py'
    tree = ast.parse(source.read_text())
    wanted = {'RetryConfigRequest', 'CreateCampaignRequest'}
    selected = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name in wanted]
    namespace = dict(BaseModel=BaseModel, Field=Field, Optional=Optional, UUID=UUID,
                     field_validator=field_validator, model_validator=model_validator,
                     datetime=datetime, ZoneInfo=ZoneInfo, MAX_SYSTEM_CONCURRENCY=100,
                     ScheduleConfigRequest=dict, CircuitBreakerConfigRequest=dict, CallingHoursConfigRequest=dict)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace['CreateCampaignRequest']


def test_legacy_campaign_request_keeps_default_retry_and_schedule_contract():
    model = request_model()(name='Legacy', workflow_id=10, source_type='csv', source_id='file.csv')
    assert model.verified_caller_handoff_id is None
    assert model.retry_config is None


def test_pool_campaign_requires_disabled_provider_retries():
    model = request_model()
    common = dict(name='Pool', workflow_id=10, source_type='csv', source_id='file.csv', verified_caller_handoff_id=HANDOFF)
    for retry in [None, {'enabled': True, 'max_retries': 0}, {'enabled': False, 'max_retries': 2}]:
        with pytest.raises(ValidationError, match='provider retries disabled'):
            model(**common, retry_config=retry)
    assert str(model(**common, retry_config={'enabled': False, 'max_retries': 0}).verified_caller_handoff_id) == HANDOFF


def test_pool_campaign_cannot_self_schedule_before_binding():
    with pytest.raises(ValidationError, match='before scheduling'):
        request_model()(name='Pool', workflow_id=10, source_type='csv', source_id='file.csv',
                        verified_caller_handoff_id=HANDOFF, retry_config={'enabled': False, 'max_retries': 0},
                        scheduled_start_at=datetime(2030, 1, 1, tzinfo=timezone.utc), scheduled_timezone='UTC')


@pytest.mark.parametrize('marked', [False, True])
def test_gate_payload_preserves_old_fields_and_adds_only_marked_context(monkeypatch, marked):
    monkeypatch.setenv('SYSEVO_PRE_CALL_CHECK_URL', 'https://example.invalid/check')
    monkeypatch.setenv('SYSEVO_MEMORY_SECRET', 'synthetic-only')
    with patch('api.services.dial_permission_check.httpx.AsyncClient') as factory:
        client = AsyncMock()
        response = Mock(is_success=True)
        response.json.return_value = {'call_inbound': {'dynamic_variables': {'dial_blocked': 'true', 'dial_block_reason': 'verified_caller_unavailable'}}}
        client.post.return_value = response
        factory.return_value.__aenter__.return_value = client
        kwargs = dict(campaign_id=501, queued_run_id=701, verified_caller_handoff_id=HANDOFF) if marked else {}
        outcome = asyncio.run(check_dial_permitted(10, '+15550000101', {'mode': 'inherit'}, **kwargs))
        assert outcome == (False, 'verified_caller_unavailable', None)
        body = client.post.call_args.kwargs['json']['call_inbound']
        assert body == {'agent_id': 10, 'to_number': '+15550000101', 'calling_hours': {'mode': 'inherit'}, **kwargs}


def load_actual_function(relative, name, namespace, parent=None):
    source = Path(__file__).resolve().parents[2] / relative
    tree = ast.parse(source.read_text())
    body = next(node.body for node in tree.body if isinstance(node, ast.ClassDef) and node.name == parent) if parent else tree.body
    node = next(node for node in body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    node.decorator_list = []
    namespace.update(Optional=Optional, datetime=datetime)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace[name]


def test_actual_campaign_client_persists_marker_with_existing_metadata():
    session = SimpleNamespace(add=Mock(), commit=AsyncMock(), rollback=AsyncMock(), refresh=AsyncMock(), execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)))
    context = AsyncMock()
    context.__aenter__.return_value = session
    client = SimpleNamespace(async_session=lambda: context)
    model = type('Campaign', (SimpleNamespace,), {'organization_id': MagicMock()})
    query = MagicMock(); query.where.return_value = query; query.params.return_value = query
    function = load_actual_function('db/campaign_client.py', 'create_campaign', {'CampaignModel': model, 'text': lambda sql: sql, 'select': lambda _: query, 'CallerHandoffConflict': ValueError}, 'CampaignClient')
    campaign = asyncio.run(function(client, name='Pool', workflow_id=10, source_type='csv', source_id='file.csv',
                                   user_id=1, organization_id=42, retry_config={'enabled': False, 'max_retries': 0},
                                   max_concurrency=2, calling_hours_config={'mode': 'inherit'}, verified_caller_handoff_id=HANDOFF))
    assert campaign.orchestrator_metadata == {'verified_caller_handoff_id': HANDOFF, 'max_concurrency': 2, 'calling_hours_mode': 'inherit'}
    assert campaign.state == 'created' and campaign.organization_id == 42
    session.add.assert_called_once_with(campaign)
    session.commit.assert_awaited_once()
    assert session.execute.await_args_list[0].args == (
        'SELECT pg_advisory_xact_lock(hashtextextended(:handoff_lock, 0))',
        {'handoff_lock': f'crm-caller:42:{HANDOFF}'},
    )
    query.params.assert_called_once_with(handoff_id=HANDOFF)



def test_actual_campaign_response_exposes_account_identity_and_marker():
    source = Path(__file__).resolve().parents[2] / 'routes/campaign.py'
    tree = ast.parse(source.read_text())
    wanted = {'RetryConfigResponse', 'TimeSlotResponse', 'ScheduleConfigResponse', 'CallingHoursConfigResponse', 'CircuitBreakerConfigResponse', 'CampaignLogEntryResponse', 'CampaignResponse'}
    namespace = dict(BaseModel=BaseModel, Field=Field, Optional=Optional, List=List, Dict=Dict, Any=Any, datetime=datetime)
    nodes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name in wanted]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    function = load_actual_function('routes/campaign.py', '_build_campaign_response', namespace)
    campaign = SimpleNamespace(id=501, name='Pool', organization_id=42, workflow_id=10, state='created', source_type='csv', source_id='file.csv',
                               total_rows=1, processed_rows=0, failed_rows=0, suppressed_rows=0, created_at=datetime(2026, 11, 7, tzinfo=timezone.utc),
                               started_at=None, completed_at=None, scheduled_start_at=None, scheduled_timezone=None, telephony_configuration_id=None, logs=[],
                               retry_config=dict(enabled=False, max_retries=0, retry_delay_seconds=120, retry_on_busy=True, retry_on_no_answer=True, retry_on_voicemail=True),
                               orchestrator_metadata={'verified_caller_handoff_id': HANDOFF, 'calling_hours_mode': 'inherit', 'max_concurrency': 2})
    response = function(campaign, 'Caller agent')
    assert response.organization_id == 42 and response.verified_caller_handoff_id == HANDOFF
    assert response.calling_hours.mode == 'inherit' and response.max_concurrency == 2
    assert response.retry_config.enabled is False and response.workflow_name == 'Caller agent'


class _HTTPError(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


def test_identity_exposes_only_the_current_authenticated_org():
    db = SimpleNamespace(get_organization_by_id=AsyncMock(return_value=SimpleNamespace(id=42, provider_id='supabase_org_acct_test')))
    function = load_actual_function('routes/organization.py', 'get_organization_identity', {'db_client': db, 'UserModel': object, 'Depends': lambda _: None, 'get_user': None, 'HTTPException': _HTTPError})
    assert asyncio.run(function(SimpleNamespace(selected_organization_id=42))) == {'organization_id': 42, 'provider_id': 'supabase_org_acct_test'}
    db.get_organization_by_id.assert_awaited_once_with(42)
    with pytest.raises(_HTTPError):
        asyncio.run(function(SimpleNamespace(selected_organization_id=None)))


@pytest.mark.parametrize('change', ['retry', 'agent', 'redial'])
def test_pool_campaign_updates_and_redial_cannot_bypass_the_pool(change):
    campaign = SimpleNamespace(workflow_id=10, state='completed', orchestrator_metadata={'verified_caller_handoff_id': HANDOFF})
    db = SimpleNamespace(get_campaign=AsyncMock(return_value=campaign))
    namespace = {'db_client': db, 'UserModel': object, 'Request': object, 'UpdateCampaignRequest': object, 'RedialCampaignRequest': object,
                 'Depends': lambda _: None, 'get_user': None, 'HTTPException': _HTTPError, 'CampaignResponse': dict}
    function = load_actual_function('routes/campaign.py', 'redial_campaign' if change == 'redial' else 'update_campaign', namespace)
    user = SimpleNamespace(is_superuser=False, selected_organization_id=42)
    with pytest.raises(_HTTPError) as caught:
        if change == 'redial':
            asyncio.run(function(501, None, user))
        else:
            request = SimpleNamespace(retry_config=SimpleNamespace(enabled=True, max_retries=2) if change == 'retry' else None, workflow_id=11 if change == 'agent' else None)
            asyncio.run(function(501, request, None, user))
    assert caught.value.status_code == 409
    db.get_campaign.assert_awaited_once_with(501, 42)

@pytest.mark.parametrize('fields,expected', [
    ({'dial_blocked': 'false'}, False),
    ({'dial_blocked': 'false', 'verified_caller_allowed': 'true'}, True),
    ({'dial_blocked': 'false', 'verified_caller_allowed': True}, False),
    ({}, False),
])
def test_marked_gate_requires_positive_permission(monkeypatch, fields, expected):
    monkeypatch.setenv('SYSEVO_PRE_CALL_CHECK_URL', 'https://example.invalid/check')
    monkeypatch.setenv('SYSEVO_DIAL_CHECK_FAIL_OPEN', 'true')
    with patch('api.services.dial_permission_check.httpx.AsyncClient') as factory:
        client = AsyncMock()
        client.post.return_value = Mock(is_success=True)
        client.post.return_value.json.return_value = {'call_inbound': {'dynamic_variables': fields}}
        factory.return_value.__aenter__.return_value = client
        outcome = asyncio.run(check_dial_permitted(10, '+15550000101', campaign_id=501,
                              queued_run_id=701, verified_caller_handoff_id=HANDOFF))
        assert outcome[0] is expected
        if not expected:
            assert outcome[1] == 'check_unavailable' and outcome[2]


def test_marked_missing_endpoint_defers_while_legacy_contract_stays_open(monkeypatch):
    monkeypatch.delenv('SYSEVO_PRE_CALL_CHECK_URL', raising=False)
    monkeypatch.setenv('SYSEVO_DIAL_CHECK_FAIL_OPEN', 'true')
    assert asyncio.run(check_dial_permitted(10, '+15550000101')) == (True, '', None)
    result = asyncio.run(check_dial_permitted(10, '+15550000101', campaign_id=501,
                         queued_run_id=701, verified_caller_handoff_id=HANDOFF))
    assert result[0] is False and result[1] == 'check_unavailable' and result[2]


@pytest.mark.parametrize('failure', ['http', 'timeout', 'malformed'])
def test_marked_gate_never_uses_legacy_fail_open_on_unavailable_check(monkeypatch, failure):
    import httpx
    monkeypatch.setenv('SYSEVO_PRE_CALL_CHECK_URL', 'https://example.invalid/check')
    monkeypatch.setenv('SYSEVO_DIAL_CHECK_FAIL_OPEN', 'true')
    with patch('api.services.dial_permission_check.httpx.AsyncClient') as factory:
        client = AsyncMock()
        response = Mock(is_success=failure != 'http', status_code=503)
        response.json.return_value = []
        client.post.return_value = response
        if failure == 'timeout':
            client.post.side_effect = httpx.ReadTimeout('synthetic timeout')
        factory.return_value.__aenter__.return_value = client
        result = asyncio.run(check_dial_permitted(10, '+15550000101', campaign_id=501,
                             queued_run_id=701, verified_caller_handoff_id=HANDOFF))
        assert result[0] is False and result[1] == 'check_unavailable' and result[2]
        assert asyncio.run(check_dial_permitted(10, '+15550000101')) == (True, '', None)
