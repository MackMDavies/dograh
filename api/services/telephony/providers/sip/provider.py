"""SIP registration and media through the managed Asterisk gateway."""
import aiohttp
from fastapi import HTTPException
from api.services.telephony.asterisk_media import AsteriskMediaProvider
from api.services.telephony.base import ProviderSyncResult


class SIPProvider(AsteriskMediaProvider):
    PROVIDER_NAME='sip'

    def __init__(self,config):
        super().__init__(config)
        self.number=config['number']
        self.domain=config['domain']
        self.username=config['username']
        self.gateway_id=config['gateway_id']

    async def registration_status(self):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as session:
                async with session.get(f'http://sysevo-sip-gateway:8089/connections/{self.gateway_id}',auth=aiohttp.BasicAuth(self.app_name,self.app_password),allow_redirects=False) as response:
                    if response.status!=200:return False
                    result=await response.json()
                    return result.get('registered') is True
        except (aiohttp.ClientError,TimeoutError):
            return False

    async def get_available_phone_numbers(self):
        if not await self.registration_status():
            raise HTTPException(status_code=409,detail='The SIP account has not registered yet. Check its hostname, username and password, then retry.')
        return [self.number]

    async def configure_inbound(self,address,webhook_url):
        return ProviderSyncResult(ok=address==self.number and await self.registration_status(),message='SIP routing requires a registered trunk and its configured number.')

    async def initiate_call(self,to_number,webhook_url,**kwargs):
        return await super().initiate_call(f'PJSIP/{to_number}@{self.app_name}',webhook_url,**kwargs)

    async def transfer_call(self,destination,*args,**kwargs):
        return await super().transfer_call(f'PJSIP/{destination}@{self.app_name}',*args,**kwargs)
