"""SignalWire cXML streaming uses the documented PCMU media/clear wire format."""
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from api.services.telephony.factory import load_credentials_for_transport
from api.services.pipecat.audio_mixer import build_audio_out_mixer
from api.services.pipecat.transport_params import realtime_param_overrides
from .provider import SignalWireProvider


class SignalWireHangup:
    def __init__(self, config):
        self.provider = SignalWireProvider(config)

    async def execute_hangup(self, context):
        from urllib.parse import quote
        await self.provider._request('POST', f'/Calls/{quote(context["call_sid"], safe="")}.json', data={'Status': 'completed'})
        return True


async def create_transport(websocket, workflow_run_id, audio_config, organization_id, *, ambient_noise_config=None, telephony_configuration_id=None, is_realtime=False, stream_sid, call_sid):
    config = await load_credentials_for_transport(organization_id, telephony_configuration_id, expected_provider='signalwire')
    serializer = TwilioFrameSerializer(stream_sid=stream_sid, call_sid=call_sid, account_sid=config['project_id'], auth_token=config['api_token'], hangup_strategy=SignalWireHangup(config))
    mixer = await build_audio_out_mixer(audio_config.transport_out_sample_rate, ambient_noise_config)
    return FastAPIWebsocketTransport(websocket=websocket, params=FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True, audio_in_sample_rate=audio_config.transport_in_sample_rate, audio_out_sample_rate=audio_config.transport_out_sample_rate, audio_out_mixer=mixer, serializer=serializer, **realtime_param_overrides(is_realtime)))
