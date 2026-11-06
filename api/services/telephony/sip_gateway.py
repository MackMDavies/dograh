"""Private SIP gateway controller. Credentials remain in native encrypted storage.

Run only in the dedicated gateway container; never in the live API process.
ARI and this control API must have no host port mappings.
"""
import asyncio
import contextlib
import hmac
import os
import ipaddress
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from loguru import logger

from api.services.telephony.sip_gateway_config import (
    gateway_identity, registration_is_ready, render_gateway_connections,
)


class Gateway:
    def __init__(self, directory: Path):
        self.directory = directory
        self.connections = {}
        self.rendered = None
        self.process = None
        self.ready = False

    async def command(self, command: str) -> str:
        process = await asyncio.create_subprocess_exec(
            'asterisk', '-rx', command, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 5)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError('Gateway command timed out') from None
        if process.returncode:
            raise RuntimeError('Gateway command failed')
        return output.decode(errors='replace')

    async def refresh(self):
        from api.db import db_client
        rows = await db_client.list_all_telephony_configurations_by_provider('sip')
        connections = {}
        for row in rows:
            # One malformed account must not disable unrelated trunks.
            try:
                render_gateway_connections([{'credentials': row.credentials}])
                connections[row.credentials['gateway_id']] = row.credentials
            except (ValueError, KeyError, TypeError):
                logger.warning('Skipping invalid SIP configuration id={}', row.id)
        rendered = render_gateway_connections([{'credentials': c} for c in connections.values()])
        if rendered != self.rendered:
            for name, content in rendered.items():
                temporary = self.directory / (name + '.next')
                # The directory is a container tmpfs. No secrets enter the image,
                # release artifact, stdout, or persistent host storage.
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, 'w') as output:
                    output.write(content)
                temporary.replace(self.directory / name)
            if self.process:
                for command in ('pjsip reload', 'module reload res_ari.so', 'dialplan reload'):
                    await self.command(command)
            self.rendered = rendered
        self.connections = connections

    async def poll(self):
        while True:
            await asyncio.sleep(3)
            try:
                await self.refresh()
            except Exception:
                # Keep the last good registration during DB/network outages.
                # Do not log exceptions containing decrypted credentials.
                logger.warning('SIP gateway refresh failed; retaining last valid configuration')


gateway = Gateway(Path('/etc/asterisk'))
security = HTTPBasic()


def write_base_configuration(directory: Path):
    public_ip = ipaddress.ip_address(os.environ['SYSEVO_SIP_PUBLIC_IP'])
    if public_ip.version != 4 or not public_ip.is_global:
        raise RuntimeError('Gateway requires a public IPv4 address')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    files = {
        'asterisk.conf': '[directories]\nastetcdir => /etc/asterisk\nastmoddir => /usr/lib/asterisk/modules\nastvarlibdir => /var/lib/asterisk\nastdbdir => /var/lib/asterisk/db\nastspooldir => /var/spool/asterisk\nastrundir => /var/run/asterisk\nastlogdir => /var/log/asterisk\n[options]\nverbose=0\ndebug=0\n',
        'modules.conf': '[modules]\nautoload=yes\nnoload=chan_sip.so\nnoload=res_pjsip_endpoint_identifier_anonymous.so\nnoload=app_externalivr.so\nnoload=app_system.so\nnoload=app_dial.so\nnoload=res_clioriginate.so\n',
        'http.conf': '[general]\nenabled=yes\nbindaddr=0.0.0.0\nbindport=8088\n',
        'ari.conf': '[general]\nenabled=yes\npretty=no\n#include ari_connections.conf\n',
        'extensions.conf': '#include extensions_connections.conf\n',
        'pjsip.conf': '[global]\ntype=global\nendpoint_identifier_order=line,username,auth_username\n[transport-udp]\ntype=transport\nprotocol=udp\nbind=0.0.0.0:5060\nexternal_signaling_address=' + str(public_ip) + '\nexternal_media_address=' + str(public_ip) + '\nlocal_net=172.16.0.0/12\nlocal_net=10.0.0.0/8\nlocal_net=192.168.0.0/16\n#include pjsip_connections.conf\n',
        'rtp.conf': '[general]\nrtpstart=30000\nrtpend=30199\nicesupport=no\nstrictrtp=yes\n',
        'websocket_client.conf': '[sysevo_media]\ntype=websocket_client\nuri=wss://api.sysevo.io/api/v1/telephony/ws/ari\nprotocols=media\ntls_enabled=yes\nverify_server_cert=yes\nverify_server_hostname=yes\nca_list_file=/etc/ssl/certs/ca-certificates.crt\nconnection_type=per_call_config\n',
        'logger.conf': '[general]\n[logfiles]\n',
    }
    for name, content in files.items():
        path = directory / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w') as output:
            output.write(content)
    for name in ('/var/run/asterisk', '/var/log/asterisk', '/var/spool/asterisk', '/var/lib/asterisk/db'):
        Path(name).mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app):
    if os.getenv('SYSEVO_SIP_GATEWAY_ENABLED', 'false').lower() != 'true':
        raise RuntimeError('SIP gateway is disabled')
    required = ('DATABASE_URL', 'REDIS_URL', 'OSS_JWT_SECRET', 'SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY', 'SYSEVO_PRE_CALL_CHECK_URL', 'SYSEVO_MEMORY_SECRET')
    if any(not os.getenv(name) for name in required):
        raise RuntimeError('Gateway billing and tenant configuration is incomplete')
    write_base_configuration(gateway.directory)
    await gateway.refresh()
    gateway.process = await asyncio.create_subprocess_exec(
        'asterisk', '-f', '-g', stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    for _ in range(40):
        if gateway.process.returncode is not None:
            raise RuntimeError('Asterisk failed to start')
        try:
            await gateway.command('core show version')
            gateway.ready = True
            break
        except RuntimeError:
            await asyncio.sleep(.25)
    if not gateway.ready:
        gateway.process.terminate()
        await gateway.process.wait()
        raise RuntimeError('Asterisk did not become ready')
    from api.services.telephony.ari_manager import ARIManager
    manager = ARIManager(provider_name="sip", refresh_interval=3)
    tasks = [asyncio.create_task(gateway.poll()), asyncio.create_task(manager.start())]
    try:
        yield
    finally:
        gateway.ready = False
        await manager.stop()
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        gateway.process.terminate()
        await gateway.process.wait()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get('/health')
async def health():
    if not gateway.ready or gateway.process.returncode is not None:
        raise HTTPException(status_code=503, detail='Gateway unavailable')
    return {'ready': True}


@app.get('/connections/{connection_id}')
async def connection_status(connection_id: str, credentials: HTTPBasicCredentials = Depends(security)):
    stored = gateway.connections.get(connection_id)
    if stored is None:
        raise HTTPException(status_code=401, detail='Unknown connection')
    username, password = gateway_identity(stored)
    if not (hmac.compare_digest(username, credentials.username) and hmac.compare_digest(password, credentials.password)):
        raise HTTPException(status_code=401, detail='Invalid connection credentials')
    try:
        output = await gateway.command('pjsip show registration ' + username + '_registration')
    except RuntimeError:
        raise HTTPException(status_code=503, detail='Gateway unavailable') from None
    registered = registration_is_ready(output)
    if registered:
        # REGISTER alone does not prove the event listener can route a call.
        import aiohttp
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
                async with session.get('http://127.0.0.1:8088/ari/applications/' + username, auth=aiohttp.BasicAuth(username, password), allow_redirects=False) as response:
                    registered = response.status == 200
        except (aiohttp.ClientError, TimeoutError):
            registered = False
    return {'registered': registered}
