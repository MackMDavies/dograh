"""Vonage call operation strategies."""

import time
from typing import Any, Dict

import aiohttp
import jwt
from loguru import logger
from pipecat.serializers.call_strategies import TransferStrategy

from api.services.telephony.warm_transfer import vonage_conversation_ncco

VONAGE_API = "https://api.nexmo.com"


def _jwt(application_id: str, private_key: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"application_id": application_id, "iat": now, "exp": now + 300, "jti": str(time.time())},
        private_key,
        algorithm="RS256",
    )


class VonageConversationStrategy(TransferStrategy):
    """Moves the caller's leg into the transfer's named conversation, which the
    colleague joined (or joins on pressing 1). Replacing the call's NCCO ends the
    websocket leg, so the agent drops out as the two are connected.
    https://developer.vonage.com/en/api/voice#updateCall
    """

    async def execute_transfer(self, context: Dict[str, Any]) -> bool:
        from api.services.telephony.call_transfer_manager import get_call_transfer_manager

        call_uuid = context.get("call_uuid")
        try:
            manager = await get_call_transfer_manager()
            transfer = await manager.find_transfer_context_for_call(call_uuid)
            if transfer is None or not transfer.accepted:
                logger.error(f"[Vonage Transfer] no connected transfer for call {call_uuid}")
                return False
            token = _jwt(context["application_id"], context["private_key"])
            async with aiohttp.ClientSession() as session:
                async with session.put(
                    f"{VONAGE_API}/v1/calls/{call_uuid}",
                    json={
                        "action": "transfer",
                        "destination": {"type": "ncco", "ncco": vonage_conversation_ncco(transfer.conference_name)},
                    },
                    headers={"Authorization": f"Bearer {token}"},
                ) as response:
                    ok = response.status in (200, 204)
                    if not ok:
                        logger.error(f"[Vonage Transfer] {response.status}: {await response.text()}")
            await manager.remove_transfer_context(transfer.transfer_id)
            return ok
        except Exception as e:  # noqa: BLE001
            logger.error(f"[Vonage Transfer] failed for call {call_uuid}: {e}")
            return False
