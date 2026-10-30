from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class FakeWebSocket:
    application_state = "connected"

    def __init__(self):
        self.send_json = AsyncMock()


class FakePeerConnection:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self._pc_id = None
        self.initialized = AsyncMock()

    def event_handler(self, _event):
        return lambda handler: handler

    async def initialize(self, *, sdp, type):
        await self.initialized(sdp=sdp, type=type)

    def get_answer(self):
        return {"sdp": "v=0\r\n", "type": "answer", "pc_id": self._pc_id}


@pytest.mark.asyncio
async def test_syra_webrtc_offer_skips_voice_quota_and_wallet_gates(monkeypatch):
    from api.db import db_client
    from api.routes import webrtc_signaling as signaling
    from api.services import syra_voice, workflow_active_check

    manager = signaling.SignalingManager()
    ws = FakeWebSocket()
    pc = FakePeerConnection()
    monkeypatch.setattr(db_client, "get_workflow_organization_id", AsyncMock(return_value=None))
    monkeypatch.setattr(signaling, "set_current_run_id", lambda _run_id: None)
    monkeypatch.setattr(syra_voice, "is_syra_voice_workflow", AsyncMock(return_value=True))
    monkeypatch.setattr(workflow_active_check, "check_workflow_active", AsyncMock(return_value=(True, None)))
    quota = AsyncMock(side_effect=AssertionError("Syra must not use billable quota"))
    wallet = AsyncMock(side_effect=AssertionError("Syra must not use the customer wallet"))
    monkeypatch.setattr(signaling, "check_dograh_quota", quota)
    monkeypatch.setattr(signaling, "check_wallet_before_call", wallet)
    monkeypatch.setattr(signaling, "get_ice_servers", lambda **_kwargs: [])
    monkeypatch.setattr(signaling, "SmallWebRTCConnection", lambda **_kwargs: pc)
    monkeypatch.setattr(signaling, "register_ws_sender", lambda *_args: None)

    def discard_task(coro):
        coro.close()

    monkeypatch.setattr(signaling.asyncio, "create_task", discard_task)

    await manager._handle_offer(
        ws,
        {"pc_id": "syra-test", "sdp": "offer", "type": "offer"},
        workflow_id=12,
        workflow_run_id=34,
        user=SimpleNamespace(id=56, provider_id=78),
    )

    quota.assert_not_awaited()
    wallet.assert_not_awaited()
    pc.initialized.assert_awaited_once_with(sdp="offer", type="offer")
    ws.send_json.assert_awaited_once()
    assert ws.send_json.await_args.args[0]["type"] == "answer"


@pytest.mark.asyncio
async def test_ordinary_webrtc_offer_still_obeys_dograh_quota(monkeypatch):
    from api.db import db_client
    from api.routes import webrtc_signaling as signaling
    from api.services import syra_voice

    manager = signaling.SignalingManager()
    ws = FakeWebSocket()
    monkeypatch.setattr(db_client, "get_workflow_organization_id", AsyncMock(return_value=None))
    monkeypatch.setattr(signaling, "set_current_run_id", lambda _run_id: None)
    monkeypatch.setattr(syra_voice, "is_syra_voice_workflow", AsyncMock(return_value=False))
    quota = AsyncMock(
        return_value=SimpleNamespace(
            has_quota=False,
            error_code="quota_exceeded",
            error_message="Quota exceeded",
        )
    )
    wallet = AsyncMock()
    monkeypatch.setattr(signaling, "check_dograh_quota", quota)
    monkeypatch.setattr(signaling, "check_wallet_before_call", wallet)

    await manager._handle_offer(
        ws,
        {"pc_id": "ordinary-test", "sdp": "offer", "type": "offer"},
        workflow_id=13,
        workflow_run_id=35,
        user=SimpleNamespace(id=57, provider_id=79),
    )

    quota.assert_awaited_once_with(SimpleNamespace(id=57, provider_id=79), workflow_id=13)
    wallet.assert_not_awaited()
    assert ws.send_json.await_args.args[0]["payload"]["error_type"] == "quota_exceeded"
