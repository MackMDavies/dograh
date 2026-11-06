"""Register the customer-owned SignalWire AI voice provider."""
from api.services.telephony.registry import ProviderSpec, ProviderUIField, ProviderUIMetadata, register
from .config import SignalWireConfigurationRequest, SignalWireConfigurationResponse
from .provider import SignalWireProvider
from .transport import create_transport

SPEC = ProviderSpec(name='signalwire', provider_cls=SignalWireProvider, config_loader=lambda value: {'provider': 'signalwire', **{key: value.get(key) for key in ('space_url', 'project_id', 'api_token', 'signing_key')}}, transport_factory=create_transport, transport_sample_rate=8000, config_request_cls=SignalWireConfigurationRequest, config_response_cls=SignalWireConfigurationResponse, account_id_credential_field='project_id', ui_metadata=ProviderUIMetadata(display_name='SignalWire', docs_url='https://signalwire.com/docs/compatibility-api', fields=[
    ProviderUIField(name='space_url', label='Space URL', type='text', placeholder='yourspace.signalwire.com'),
    ProviderUIField(name='project_id', label='Project ID', type='text', sensitive=True, placeholder='xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'),
    ProviderUIField(name='api_token', label='API token', type='password', sensitive=True, placeholder='Your API token'),
    ProviderUIField(name='signing_key', label='Signing key', type='password', sensitive=True, placeholder='Webhook signing key'),
]))
register(SPEC)
