"""Read-only import/serializer compatibility probe in a network-none container."""
import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic import TypeAdapter
from api.schemas.telephony_config import TelephonyConfigRequest
from api.services.telephony import registry
from api.services.telephony.call_concurrency import is_live_call_mode
from api.services.pipecat.audio_config import create_audio_config
from api.services.telephony.providers.signalwire import transport as signalwire
from api.services.telephony.providers.sip import transport as sip
from api.services.telephony.sip_gateway_config import asterisk_config


async def main():
    signalwire_config = {'provider': 'signalwire', 'space_url': 'fixture.signalwire.com', 'project_id': '12345678-1234-1234-1234-123456789012', 'api_token': 'fixture-token', 'signing_key': 'fixture-signing-key'}
    sip_config = {'provider': 'sip', 'domain': 'fixture.test', 'username': '1001', 'password': 'fixture-secret', 'number': '+12125550101'}
    for config in (signalwire_config, sip_config):
        assert TypeAdapter(TelephonyConfigRequest).validate_python(config).provider == config['provider']
        assert registry.get(config['provider']).transport_sample_rate == 8000
        assert is_live_call_mode(config['provider'])
    signalwire.load_credentials_for_transport = AsyncMock(return_value=signalwire_config)
    sip.load_credentials_for_transport = AsyncMock(return_value=asterisk_config({**sip_config, 'gateway_id': 'a' * 32}))
    socket = SimpleNamespace()
    for realtime in (False, True):
        first = await signalwire.create_transport(socket, 1, create_audio_config('signalwire'), 9, stream_sid='fixture-stream', call_sid='fixture-call', telephony_configuration_id=17, is_realtime=realtime)
        second = await sip.create_transport(socket, 1, create_audio_config('sip'), 9, channel_id='fixture-channel', telephony_configuration_id=18, is_realtime=realtime)
        assert first is not None and second is not None
    signalwire.load_credentials_for_transport.assert_awaited_with(9, 17, expected_provider='signalwire')
    sip.load_credentials_for_transport.assert_awaited_with(9, 18, expected_provider='sip')
    hangup = signalwire.SignalWireHangup(signalwire_config)
    hangup.provider._request = AsyncMock(return_value={})
    assert await hangup.execute_hangup({'call_sid': 'fixture-call'})
    hangup.provider._request.assert_awaited_once_with('POST', '/Calls/fixture-call.json', data={'Status': 'completed'})
    fields = {field.name: field for field in registry.get('signalwire').ui_metadata.fields}
    assert fields['api_token'].sensitive and fields['signing_key'].sensitive
    if '--api' in sys.argv:
        from api.routes.organization import get_telephony_providers_metadata
        user = SimpleNamespace(selected_organization_id=9)
        os.environ['SYSEVO_SIP_GATEWAY_ENABLED'] = 'false'
        metadata = await get_telephony_providers_metadata(user)
        assert 'sip' not in {item.provider for item in metadata.providers}
        os.environ['SYSEVO_SIP_GATEWAY_ENABLED'] = 'true'
        metadata = await get_telephony_providers_metadata(user)
        assert {'sip', 'signalwire'} <= {item.provider for item in metadata.providers}
        from api.routes.telephony import router
        paths = {route.path for route in router.routes}
        assert '/signalwire-xml' in paths and '/signalwire/status/{workflow_run_id}' in paths
        assert '/ws/ari' in paths
    print(json.dumps({'schema_union': True, 'native_registry': True, 'native_serializers': True, 'run_scoped_credentials': True, 'tts_and_realtime_transports': True, 'signalwire_hangup': True, 'api_metadata': '--api' in sys.argv}))


asyncio.run(main())
