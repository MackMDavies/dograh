from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from api.services.telephony.dialer import transcript_store as store

CALL = 'CA' + 'a' * 32
FINAL = {'is_final': True, 'text': 'Can you hear me?', 'speaker': 'prospect', 'start': 2.5, 'duration': 1.25, 'stream_id': 'MZtest'}

@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(store, 'SUPABASE_URL', 'https://example.supabase.co')
    monkeypatch.setattr(store, 'SUPABASE_SERVICE_ROLE_KEY', 'test-key')

@pytest.mark.asyncio
async def test_stores_only_final_attributed_words_with_a_stable_identity():
    response = MagicMock(); response.json.return_value = True
    client = AsyncMock(); client.__aenter__.return_value = client; client.post.return_value = response
    with patch.object(store.httpx, 'AsyncClient', return_value=client):
        assert await store.persist_live_caption(CALL, FINAL)
        assert await store.persist_live_caption(CALL, FINAL)
        body = client.post.await_args.kwargs['json']
        assert body['p_text'] == FINAL['text']
        assert body['p_speaker'] == 'prospect'
        assert body['p_end_seconds'] == 3.75
        assert client.post.await_args_list[0].kwargs['json']['p_segment_key'] == body['p_segment_key']
        assert not await store.persist_live_caption(CALL, {**FINAL, 'is_final': False})
        assert client.post.await_count == 2

@pytest.mark.asyncio
async def test_bad_timing_and_untrusted_call_ids_never_write():
    with patch.object(store.httpx, 'AsyncClient') as client:
        assert not await store.persist_live_caption('another-call', FINAL)
        assert not await store.persist_live_caption(CALL, {**FINAL, 'start': float('nan')})
        assert not await store.persist_live_caption(CALL, {**FINAL, 'speaker': None})
        client.assert_not_called()

@pytest.mark.asyncio
async def test_persistence_failure_cannot_interrupt_a_call():
    client = AsyncMock(); client.__aenter__.return_value = client; client.post.side_effect = RuntimeError('storage unavailable')
    with patch.object(store.httpx, 'AsyncClient', return_value=client):
        assert not await store.persist_live_caption(CALL, FINAL)
