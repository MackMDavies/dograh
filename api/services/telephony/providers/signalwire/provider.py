"""SignalWire's signed cXML/PCMU voice path, with no dialer credentials."""
import asyncio
import json
import re
from urllib.parse import quote, urlsplit
from xml.sax.saxutils import quoteattr

import aiohttp
from fastapi import HTTPException, Response
from twilio.request_validator import RequestValidator

from api.services.telephony.base import CallInitiationResult, NormalizedInboundData, ProviderSyncResult, TelephonyProvider
from api.utils.common import get_backend_endpoints
from api.utils.telephony_address import normalize_telephony_address
from .config import normalize_space_url


class SignalWireProvider(TelephonyProvider):
    PROVIDER_NAME = 'signalwire'
    WEBHOOK_ENDPOINT = 'signalwire-xml'

    def __init__(self, config):
        self.space_url = normalize_space_url(config.get('space_url', ''))
        self.project_id = config.get('project_id', '')
        self.api_token = config.get('api_token', '')
        self.signing_key = config.get('signing_key', '')
        self.from_numbers = config.get('from_numbers', [])
        self.base_url = f'https://{self.space_url}/api/laml/2010-04-01/Accounts/{quote(self.project_id, safe="")}'

    def validate_config(self):
        return bool(self.space_url and self.project_id and self.api_token and self.signing_key)

    async def _request(self, method, path, *, data=None, params=None):
        # Never follow a redirect with a customer's provider credentials.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.request(method, self.base_url + path, auth=aiohttp.BasicAuth(self.project_id, self.api_token), data=data, params=params, allow_redirects=False) as response:
                if response.status not in (200, 201, 204):
                    raise HTTPException(status_code=502, detail='SignalWire rejected the request. Check your credentials and token permissions.')
                if response.status == 204:
                    return {}
                return await response.json()

    async def list_numbers(self):
        """Read inventory without changing routing or purchasing a number."""
        rows = []
        path = '/IncomingPhoneNumbers.json?PageSize=200'
        for _ in range(10):
            result = await self._request('GET', path)
            batch = result.get('incoming_phone_numbers')
            if not isinstance(batch, list):
                raise HTTPException(status_code=502, detail='SignalWire returned an invalid number list.')
            rows.extend(batch)
            next_uri = result.get('next_page_uri')
            if not next_uri:
                break
            parsed = urlsplit(next_uri)
            prefix = urlsplit(self.base_url).path + '/'
            if parsed.scheme or parsed.netloc or not parsed.path.startswith(prefix):
                raise HTTPException(status_code=502, detail='SignalWire returned an invalid pagination link.')
            path = next_uri[len(urlsplit(self.base_url).path):]
        return rows

    async def get_available_phone_numbers(self):
        return [row['phone_number'] for row in await self.list_numbers() if isinstance(row.get('phone_number'), str) and re.fullmatch(r'\+[1-9]\d{7,14}', row['phone_number'])]

    async def configure_inbound(self, address, webhook_url):
        rows = await self.list_numbers()
        row = next((item for item in rows if item.get('phone_number') == address), None)
        if not row or not isinstance(row.get('sid'), str):
            return ProviderSyncResult(ok=False, message='This number is not on the connected SignalWire project.')
        # Only voice routing changes; SMS and the separate staff dialer are untouched.
        result = await self._request('POST', f'/IncomingPhoneNumbers/{quote(row["sid"], safe="")}.json', data={'VoiceUrl': webhook_url or '', 'VoiceMethod': 'POST'})
        return ProviderSyncResult(ok=result.get('voice_url') == (webhook_url or ''), message=None if result.get('voice_url') == (webhook_url or '') else 'SignalWire has not confirmed the voice routing change.')

    async def initiate_call(self, to_number, webhook_url, workflow_run_id=None, from_number=None, **kwargs):
        from_number = from_number or (self.from_numbers[0] if self.from_numbers else None)
        if not from_number or from_number not in self.from_numbers:
            raise ValueError('Choose a number on the connected SignalWire account.')
        data = {'To': to_number, 'From': from_number, 'Url': webhook_url, 'Method': 'POST'}
        if workflow_run_id:
            backend, _ = await get_backend_endpoints()
            data.update({'StatusCallback': f'{backend}/api/v1/telephony/signalwire/status/{workflow_run_id}', 'StatusCallbackMethod': 'POST', 'StatusCallbackEvent': ['initiated', 'ringing', 'answered', 'completed']})
        result = await self._request('POST', '/Calls.json', data=data)
        return CallInitiationResult(call_id=result['sid'], status=result.get('status', 'queued'), caller_number=from_number, provider_metadata={'call_id': result['sid']})

    async def get_call_status(self, call_id):
        return await self._request('GET', f'/Calls/{quote(call_id, safe="")}.json')

    async def get_call_cost(self, call_id):
        data = await self.get_call_status(call_id)
        return {'cost_usd': abs(float(data.get('price') or 0)), 'duration': int(data.get('duration') or 0), 'status': data.get('status', 'unknown'), 'price_unit': data.get('price_unit', 'USD')}

    def parse_status_callback(self, data):
        return {'call_id': data.get('CallSid', ''), 'status': data.get('CallStatus', ''), 'from_number': data.get('From'), 'to_number': data.get('To'), 'direction': data.get('Direction'), 'duration': data.get('CallDuration') or data.get('Duration'), 'extra': data}

    async def verify_webhook_signature(self, url, params, signature):
        return bool(self.signing_key and signature and RequestValidator(self.signing_key).validate(url, params, signature))

    async def verify_inbound_signature(self, url, webhook_data, headers, body=''):
        signature = next((v for k, v in headers.items() if k.lower() == 'x-signalwire-signature'), '')
        return await self.verify_webhook_signature(url, webhook_data, signature)

    @classmethod
    def can_handle_webhook(cls, webhook_data, headers):
        return bool(any(k.lower() == 'x-signalwire-signature' for k in headers) and webhook_data.get('CallSid') and webhook_data.get('AccountSid'))

    @staticmethod
    def parse_inbound_webhook(data):
        return NormalizedInboundData(provider='signalwire', call_id=data.get('CallSid', ''), from_number=normalize_telephony_address(data.get('From', '')).canonical, to_number=normalize_telephony_address(data.get('To', '')).canonical, direction=data.get('Direction', ''), call_status=data.get('CallStatus', ''), account_id=data.get('AccountSid'), from_country=data.get('FromCountry'), to_country=data.get('ToCountry'), raw_data=data)

    @staticmethod
    def validate_account_id(config_data, webhook_account_id):
        return bool(webhook_account_id and config_data.get('project_id') == webhook_account_id)

    @staticmethod
    def _stream_xml(url):
        if not url.startswith('wss://') or urlsplit(url).query:
            raise ValueError('SignalWire requires a secure WebSocket URL without query parameters.')
        return f'<?xml version="1.0" encoding="UTF-8"?><Response><Connect><Stream url={quoteattr(url)} codec="PCMU@8000h"/></Connect><Hangup/></Response>'

    async def get_webhook_response(self, workflow_id, user_id, workflow_run_id):
        _, wss = await get_backend_endpoints()
        return self._stream_xml(f'{wss}/api/v1/telephony/ws/{workflow_id}/{user_id}/{workflow_run_id}')

    async def start_inbound_stream(self, *, websocket_url, workflow_run_id, normalized_data, backend_endpoint):
        return Response(content=self._stream_xml(websocket_url), media_type='application/xml')

    async def handle_websocket(self, websocket, workflow_id, user_id, workflow_run_id):
        from api.db import db_client
        from api.services.pipecat.run_pipeline import run_pipeline_telephony
        connected = json.loads(await asyncio.wait_for(websocket.receive_text(), 10))
        start = json.loads(await asyncio.wait_for(websocket.receive_text(), 10))
        info = start.get('start', {})
        run = await db_client.get_workflow_run_by_id(workflow_run_id)
        expected_call = (run.gathered_context or {}).get('call_id') if run else None
        media = info.get('mediaFormat', {})
        if (connected.get('event') != 'connected' or start.get('event') != 'start' or not info.get('streamSid')
            or info.get('accountSid') != self.project_id or not expected_call or info.get('callSid') != expected_call
            or media.get('encoding') != 'audio/x-mulaw' or media.get('sampleRate') != 8000 or media.get('channels') != 1):
            await websocket.close(code=4403, reason='Invalid SignalWire call stream')
            return
        await run_pipeline_telephony(websocket, provider_name=self.PROVIDER_NAME, workflow_id=workflow_id, workflow_run_id=workflow_run_id, user_id=user_id, call_id=info['callSid'], transport_kwargs={'stream_sid': info['streamSid'], 'call_sid': info['callSid']})

    @staticmethod
    def generate_error_response(error_type, message):
        return Response(content='<Response><Say>We cannot connect your call right now.</Say><Hangup/></Response>', media_type='application/xml')

    @staticmethod
    def generate_validation_error_response(error_type):
        return SignalWireProvider.generate_error_response(error_type, '')

    async def transfer_call(self, *args, **kwargs):
        raise NotImplementedError('SignalWire AI call transfers are not configured.')

    def supports_transfers(self):
        return False
