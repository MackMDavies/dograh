"""A customer-owned registered SIP trunk; the gateway is enabled separately."""
import os
import secrets
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit
from fastapi import HTTPException
from api.services.telephony.registry import ProviderSpec,ProviderUIField,ProviderUIMetadata,register
from api.services.telephony.sip_gateway_config import asterisk_config
from api.utils.url_security import validate_user_configured_service_url
from .config import SIPConfigurationRequest,SIPConfigurationResponse
from .provider import SIPProvider
from .transport import create_transport


async def prepare_credentials(credentials):
    if os.getenv('SYSEVO_SIP_GATEWAY_ENABLED','false').lower()!='true':
        raise HTTPException(status_code=503,detail='SIP connections are not enabled on this server.')
    validate_user_configured_service_url('https://'+credentials['domain'],field_name='SIP hostname')
    parsed = urlsplit('sip://' + credentials['domain'])
    try:
        answers = await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or 5060, socket.AF_INET, socket.SOCK_DGRAM), 5)
    except (socket.gaierror, TimeoutError):
        raise HTTPException(status_code=422, detail='The SIP hostname could not be resolved. Check it and retry.') from None
    addresses = {answer[4][0] for answer in answers}
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise HTTPException(status_code=422, detail='Use a publicly reachable SIP hostname.')
    # Pin SIP traffic to the validated public address; later DNS changes cannot
    # turn the registered hostname into access to private infrastructure.
    return {**credentials,'gateway_id':secrets.token_hex(16), 'resolved_ip': sorted(addresses)[0]}


SPEC=ProviderSpec(name='sip',provider_cls=SIPProvider,config_loader=asterisk_config,transport_factory=create_transport,transport_sample_rate=8000,config_request_cls=SIPConfigurationRequest,config_response_cls=SIPConfigurationResponse,preprocess_credentials_on_save=prepare_credentials,ui_metadata=ProviderUIMetadata(display_name='SIP trunk',fields=[ProviderUIField(name='domain',label='SIP domain',type='text',placeholder='sip.yourcarrier.com'),ProviderUIField(name='username',label='Username',type='text',placeholder='Trunk username'),ProviderUIField(name='password',label='Password',type='password',sensitive=True,placeholder='Trunk password'),ProviderUIField(name='number',label='Number',type='text',placeholder='+1 (415) 555-0199')]))
register(SPEC)
