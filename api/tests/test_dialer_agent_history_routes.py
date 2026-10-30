"""The HTTP boundary preserves session checks and private media responses."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from api.routes import dialer_agent_history as routes

CALL = '11111111-1111-4111-8111-111111111111'


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_user] = lambda: SimpleNamespace(id=1)
    return app


@pytest.mark.parametrize('path', ['', '/recording'])
async def test_bearer_header_is_required_even_when_session_dependency_succeeds(app,path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test') as client:
        response = await client.get(f'/dialer/agent-calls/{CALL}{path}')
    assert response.status_code == 401


async def test_details_are_not_publicly_cacheable_and_use_supplied_session(app):
    with patch.object(routes.agent_history,'details',AsyncMock(return_value={'summary':'Private summary'})) as details:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test') as client:
            response = await client.get(f'/dialer/agent-calls/{CALL}',headers={'Authorization':'Bearer rep'})
    assert response.status_code == 200 and response.json() == {'summary':'Private summary'}
    assert response.headers['cache-control'] == 'private, no-store'
    details.assert_awaited_once_with(CALL,'Bearer rep')


async def test_recording_is_authenticated_binary_without_storage_url(app):
    with patch.object(routes.agent_history,'recording',AsyncMock(return_value=b'RIFF-private-audio')):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test') as client:
            response = await client.get(f'/dialer/agent-calls/{CALL}/recording',headers={'Authorization':'Bearer rep'})
    assert response.content == b'RIFF-private-audio' and response.headers['content-type'] == 'audio/wav'
    assert response.headers['cache-control'] == 'private, no-store'


@pytest.mark.parametrize('error,status',[
    (routes.agent_history.AgentHistoryNotFound('Not found'),404),
    (routes.HistoryStorageUnavailable('credential must not leak'),503),
])
async def test_expected_read_failures_return_clear_http_status_without_credentials(app,error,status):
    with patch.object(routes.agent_history,'details',AsyncMock(side_effect=error)):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test') as client:
            response = await client.get(f'/dialer/agent-calls/{CALL}',headers={'Authorization':'Bearer rep'})
    assert response.status_code == status and 'credential' not in response.text
