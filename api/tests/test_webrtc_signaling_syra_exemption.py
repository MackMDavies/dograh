"""Syra voice web calls skip quota and wallet checks in signaling, as embed-init does.

Every other call is still gated. The exemption is decided only by
is_syra_voice_workflow (platform attestation; False on any failure).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import api.routes.webrtc_signaling as sig
import api.services.syra_voice as syra_voice
import api.services.workflow_active_check as active_check


class _Stop(Exception):
    """Raised where the real handler would create the peer connection."""


def _wire(monkeypatch, *, syra: bool, quota_ok: bool = True, wallet_ok: bool = True):
    quota = AsyncMock(return_value=SimpleNamespace(has_quota=quota_ok, error_code="quota_exceeded", error_message="No quota"))
    wallet = AsyncMock(return_value=(wallet_ok, "" if wallet_ok else "empty"))
    monkeypatch.setattr(sig, "check_dograh_quota", quota)
    monkeypatch.setattr(sig, "check_wallet_before_call", wallet)
    monkeypatch.setattr(sig.db_client, "get_workflow_organization_id", AsyncMock(return_value=None))
    monkeypatch.setattr(syra_voice, "is_syra_voice_workflow", AsyncMock(return_value=syra))
    monkeypatch.setattr(active_check, "check_workflow_active", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(sig, "get_ice_servers", lambda user_id=None: [])

    def _no_pc(*_a, **_k):
        raise _Stop()

    monkeypatch.setattr(sig, "SmallWebRTCConnection", _no_pc)
    ws = SimpleNamespace(send_json=AsyncMock())
    return quota, wallet, ws


async def _offer(ws):
    await sig.SignalingManager()._handle_offer(
        ws, {"pc_id": "pc-1", "sdp": "v=0", "type": "offer"}, 42, 7, SimpleNamespace(id=1)
    )


@pytest.mark.asyncio
async def test_syra_voice_call_skips_quota_and_wallet(monkeypatch):
    quota, wallet, ws = _wire(monkeypatch, syra=True, quota_ok=False, wallet_ok=False)
    with pytest.raises(_Stop):
        await _offer(ws)
    quota.assert_not_awaited()
    wallet.assert_not_awaited()
    ws.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_call_is_still_rejected_without_quota(monkeypatch):
    quota, wallet, ws = _wire(monkeypatch, syra=False, quota_ok=False)
    await _offer(ws)
    quota.assert_awaited_once()
    wallet.assert_not_awaited()
    assert ws.send_json.await_args.args[0]["payload"]["error_type"] == "quota_exceeded"


@pytest.mark.asyncio
async def test_ordinary_call_is_still_rejected_without_wallet_balance(monkeypatch):
    quota, wallet, ws = _wire(monkeypatch, syra=False, wallet_ok=False)
    await _offer(ws)
    wallet.assert_awaited_once()
    assert ws.send_json.await_args.args[0]["payload"]["error_type"] == "wallet_insufficient_balance"
