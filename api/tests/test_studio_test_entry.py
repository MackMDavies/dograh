from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from api.services.workflow.studio_test_entry import (
    ANNOTATION, StudioTestEntryError, apply_test_entry,
    test_entry_annotations as entry_annotations,
)
from api.services.workflow.dto import ReactFlowDTO
from api.services.workflow.workflow_graph import WorkflowGraph


def definition():
    return {'nodes': [
        {'id': 'start', 'type': 'startCall', 'position': {'x': 0, 'y': 0}, 'data': {'name': 'Start', 'is_start': True, 'prompt': 'Welcome'}},
        {'id': 'book', 'type': 'agentNode', 'position': {'x': 0, 'y': 100}, 'data': {'name': 'Book', 'prompt': 'Book a meeting'}},
        {'id': 'end', 'type': 'endCall', 'position': {'x': 0, 'y': 200}, 'data': {'name': 'End', 'is_end': True, 'prompt': 'Goodbye'}},
    ], 'edges': [{'id': 'a', 'source': 'start', 'target': 'book', 'data': {'condition': 'Continue'}}, {'id': 'b', 'source': 'book', 'target': 'end', 'data': {'condition': 'Finished'}}]}


def test_entry_changes_only_the_test_graph_and_preserves_original_start_flags():
    source = definition(); before = deepcopy(source)
    notes = entry_annotations('smallwebrtc', 'book', source, {'unrelated': 'kept'})
    graph = WorkflowGraph(ReactFlowDTO.model_validate(source))
    apply_test_entry(graph, 'smallwebrtc', notes)
    assert graph.start_node_id == 'book'
    assert graph.nodes['start'].is_start is True
    assert graph.nodes['book'].is_start is False
    assert source == before
    assert notes == {ANNOTATION: 'book', 'unrelated': 'kept'}
    normal = WorkflowGraph(ReactFlowDTO.model_validate(source))
    apply_test_entry(normal, 'smallwebrtc', {})
    assert normal.start_node_id == 'start'


@pytest.mark.parametrize('node_id', ['missing', 'end', 'global', 'trigger'])
def test_rejects_nonconversation_steps(node_id):
    with pytest.raises(StudioTestEntryError):
        entry_annotations('smallwebrtc', node_id, definition())


@pytest.mark.parametrize('mode', ['twilio', 'telnyx', 'textchat', 'campaign'])
def test_no_phone_or_text_path_can_use_a_studio_entry(mode):
    with pytest.raises(StudioTestEntryError):
        entry_annotations(mode, 'book', definition())
    graph = WorkflowGraph(ReactFlowDTO.model_validate(definition()))
    with pytest.raises(StudioTestEntryError):
        apply_test_entry(graph, mode, {ANNOTATION: 'book'})
    assert graph.start_node_id == 'start'


@pytest.mark.asyncio
async def test_real_run_pins_entry_and_preserves_definition_context_and_tenancy(db_session, async_session):
    from api.db.models import OrganizationModel, UserModel
    marker = uuid4().hex
    org = OrganizationModel(provider_id='studio-entry-'+marker)
    foreign = OrganizationModel(provider_id='studio-foreign-'+marker)
    async_session.add_all([org, foreign]); await async_session.flush()
    user = UserModel(provider_id='studio-user-'+marker, selected_organization_id=org.id)
    async_session.add(user); await async_session.flush()
    wf = await db_session.create_workflow(name='Studio entry proof', workflow_definition=definition(), user_id=user.id, organization_id=org.id)
    run = await db_session.create_workflow_run('Selected step', wf.id, 'smallwebrtc', user.id, organization_id=org.id, bypass_user_check=True, use_draft=True, test_start_node_id='book')
    assert run.annotations[ANNOTATION] == 'book'
    saved = await db_session.get_workflow(wf.id, organization_id=org.id)
    assert saved.workflow_definition == definition()
    normal = await db_session.create_workflow_run('Normal', wf.id, 'smallwebrtc', user.id, organization_id=org.id, bypass_user_check=True)
    assert ANNOTATION not in normal.annotations
    with pytest.raises(ValueError, match='not found'):
        await db_session.create_workflow_run('Foreign', wf.id, 'smallwebrtc', user.id, organization_id=foreign.id, bypass_user_check=True, test_start_node_id='book')
    with pytest.raises(StudioTestEntryError):
        await db_session.create_workflow_run('Invalid', wf.id, 'smallwebrtc', user.id, organization_id=org.id, bypass_user_check=True, test_start_node_id='missing')


def test_route_passes_entry_to_scoped_creation_and_releases_capacity_on_rejection():
    from api.routes.workflow import router
    from api.services.auth.depends import get_user
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(id=1, selected_organization_id=11, is_superuser=False)
    run = SimpleNamespace(id=1, workflow_id=5, name='Test', mode='smallwebrtc', created_at=datetime.now(UTC), definition_id=7, initial_context={}, gathered_context={})
    with patch('api.routes.workflow.db_client') as db, patch('api.services.telephony.call_concurrency.acquire_call_slot', new=AsyncMock(return_value=(True, 'slot'))) as acquire, patch('api.services.telephony.call_concurrency.bind_slot', new=AsyncMock()) as bind, patch('api.services.telephony.call_concurrency.release_slot_for_failed_start', new=AsyncMock()) as release:
        db.create_workflow_run = AsyncMock(return_value=run)
        client = TestClient(app)
        response = client.post('/workflow/5/runs', json={'mode':'smallwebrtc','name':'Test','start_node_id':'book'})
        assert response.status_code == 200, response.text
        assert db.create_workflow_run.call_args.kwargs['test_start_node_id'] == 'book'
        assert db.create_workflow_run.call_args.kwargs['organization_id'] == 11
        bind.assert_awaited_once_with(1, 11, 'slot')
        db.create_workflow_run.side_effect = StudioTestEntryError('Choose a conversation step')
        response = client.post('/workflow/5/runs', json={'mode':'smallwebrtc','name':'Test','start_node_id':'missing'})
        assert response.status_code == 400
        release.assert_awaited_once_with(11, 'slot')
