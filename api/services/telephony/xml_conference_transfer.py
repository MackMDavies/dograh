"""Transfers for Plivo and Vobiz (Vobiz speaks Plivo's API and XML).

The colleague's leg is an outbound call whose answer_url returns conference XML (or,
for a warm transfer, the introduction inside <GetDigits>). Its hangup_url reports how
the leg ended. At hand-off the caller's leg is moved into the same conference with
the Transfer Call API (aleg_url → transfer-join). Outcome rules are the shared ones in
warm_transfer.py.

    POST /telephony/{carrier}/transfer-answer/{id}   colleague answered → XML
    POST /telephony/{carrier}/transfer-accept/{id}   their key → conference or hang up
    POST /telephony/{carrier}/transfer-hangup/{id}   how their leg ended
    POST /telephony/{carrier}/transfer-join/{id}     the caller's leg → conference

https://www.plivo.com/docs/voice/api/call/make-a-call
https://www.plivo.com/docs/voice/api/call/transfer-a-call
"""

from __future__ import annotations

import random
from typing import Any, Dict, Optional
from xml.sax.saxutils import escape

import aiohttp
from fastapi import APIRouter, Request, Response
from loguru import logger
from pipecat.serializers.call_strategies import TransferStrategy

from api.services.telephony.warm_transfer import (
    ACCEPT_TIMEOUT_SECONDS,
    DECLINED_GOODBYE,
    briefing_speech,
    report_transfer_accept,
    report_transfer_leg,
)
from api.utils.common import get_backend_endpoints

CARRIERS = {
    "plivo": "https://api.plivo.com/v1/Account/{auth_id}",
    "vobiz": "https://api.vobiz.ai/api/v1/Account/{auth_id}",
}

# Plivo hangup_url CallStatus → shared leg outcome.
_HANGUP_OUTCOMES = {
    "completed": "completed",
    "busy": "busy",
    "no-answer": "no_answer",
    "timeout": "no_answer",
    "cancel": "no_answer",
    "failed": "failed",
}


def _auth(carrier: str, auth_id: str, auth_token: str) -> Dict[str, Any]:
    if carrier == "vobiz":
        return {"headers": {"X-Auth-ID": auth_id, "X-Auth-Token": auth_token}}
    return {"auth": aiohttp.BasicAuth(auth_id, auth_token)}


def conference_xml(conference: str, greeting: Optional[str] = None) -> str:
    speak = f"<Speak>{escape(greeting)}</Speak>" if greeting else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f'{speak}<Conference endConferenceOnExit="true" startConferenceOnEnter="true" waitSound="">'
        f"{escape(conference)}</Conference></Response>"
    )


def warm_xml(introduction: str, accept_url: str) -> str:
    url = escape(accept_url)
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f'<GetDigits action="{url}" method="POST" numDigits="1" timeout="{ACCEPT_TIMEOUT_SECONDS}" retries="1">'
        f"<Speak>{escape(briefing_speech(introduction))}</Speak></GetDigits>"
        f'<Redirect method="POST">{url}</Redirect></Response>'
    )


def hangup_xml(message: str = DECLINED_GOODBYE) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Speak>{escape(message)}</Speak><Hangup/></Response>'


async def dial_colleague(
    carrier: str, *, auth_id: str, auth_token: str, from_numbers: list[str],
    destination: str, transfer_id: str, timeout: int,
) -> Dict[str, Any]:
    backend, _ = await get_backend_endpoints()
    base = f"{backend}/api/v1/telephony/{carrier}"
    data = {
        "from": random.choice(from_numbers).lstrip("+"),
        "to": destination.lstrip("+"),
        "answer_url": f"{base}/transfer-answer/{transfer_id}",
        "answer_method": "POST",
        "hangup_url": f"{base}/transfer-hangup/{transfer_id}",
        "hangup_method": "POST",
        "ring_timeout": int(timeout),
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{CARRIERS[carrier].format(auth_id=auth_id)}/Call/", json=data, **_auth(carrier, auth_id, auth_token)
        ) as response:
            body = await response.json(content_type=None)
            if response.status not in (200, 201, 202):
                raise Exception(f"{carrier} transfer call failed ({response.status}): {body}")
    leg = body.get("request_uuid") or body.get("call_uuid") or (body.get("call_uuids") or [None])[0]
    return {"call_sid": leg, "status": "initiated", "provider": carrier}


class XmlConferenceStrategy(TransferStrategy):
    """Moves the caller's leg into the transfer's conference at hand-off."""

    def __init__(self, carrier: str):
        self._carrier = carrier

    async def execute_transfer(self, context: Dict[str, Any]) -> bool:
        from api.services.telephony.call_transfer_manager import get_call_transfer_manager

        call_id, auth_id, auth_token = context.get("call_id"), context.get("auth_id"), context.get("auth_token")
        try:
            manager = await get_call_transfer_manager()
            transfer = await manager.find_transfer_context_for_call(call_id)
            if transfer is None or not transfer.accepted:
                logger.error(f"[{self._carrier} Transfer] no connected transfer for call {call_id}")
                return False
            backend, _ = await get_backend_endpoints()
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{CARRIERS[self._carrier].format(auth_id=auth_id)}/Call/{call_id}/",
                    json={
                        "legs": "aleg",
                        "aleg_url": f"{backend}/api/v1/telephony/{self._carrier}/transfer-join/{transfer.transfer_id}",
                        "aleg_method": "POST",
                    },
                    **_auth(self._carrier, auth_id, auth_token),
                ) as response:
                    ok = response.status in (200, 202)
                    if not ok:
                        logger.error(f"[{self._carrier} Transfer] {response.status}: {await response.text()}")
            return ok
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{self._carrier} Transfer] failed for call {call_id}: {e}")
            return False


def _xml(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


def build_router(carrier: str) -> APIRouter:
    router = APIRouter()

    @router.post(f"/{carrier}/transfer-answer/{{transfer_id}}", include_in_schema=False)
    async def transfer_answer(transfer_id: str, request: Request):
        from api.services.telephony.call_transfer_manager import get_call_transfer_manager

        form = dict(await request.form())
        leg = str(form.get("CallUUID") or "")
        context = await (await get_call_transfer_manager()).get_transfer_context(transfer_id)
        if context is None or context.resolved:
            return _xml(hangup_xml("This transfer is no longer waiting. Goodbye."))
        await report_transfer_leg(transfer_id, "answered", leg)
        if context.warm and context.introduction:
            backend, _ = await get_backend_endpoints()
            return _xml(warm_xml(context.introduction, f"{backend}/api/v1/telephony/{carrier}/transfer-accept/{transfer_id}"))
        return _xml(conference_xml(context.conference_name, "You have answered a transfer call. Connecting you now."))

    @router.post(f"/{carrier}/transfer-accept/{{transfer_id}}", include_in_schema=False)
    async def transfer_accept(transfer_id: str, request: Request):
        form = dict(await request.form())
        conference = await report_transfer_accept(transfer_id, str(form.get("Digits") or ""), str(form.get("CallUUID") or ""))
        return _xml(conference_xml(conference, "Connecting you now.") if conference else hangup_xml())

    @router.post(f"/{carrier}/transfer-hangup/{{transfer_id}}", include_in_schema=False)
    async def transfer_hangup(transfer_id: str, request: Request):
        form = dict(await request.form())
        outcome = _HANGUP_OUTCOMES.get(str(form.get("CallStatus") or "").lower(), "failed")
        return {"status": await report_transfer_leg(transfer_id, outcome, str(form.get("CallUUID") or ""))}

    @router.post(f"/{carrier}/transfer-join/{{transfer_id}}", include_in_schema=False)
    async def transfer_join(transfer_id: str):
        from api.services.telephony.call_transfer_manager import get_call_transfer_manager

        manager = await get_call_transfer_manager()
        context = await manager.get_transfer_context(transfer_id)
        if context is None or not context.accepted:
            return _xml(hangup_xml("Sorry, the person is no longer available. Goodbye."))
        await manager.remove_transfer_context(transfer_id)
        return _xml(conference_xml(context.conference_name))

    return router
