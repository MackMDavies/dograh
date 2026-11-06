"""Real Asterisk probe, ONLY inside an isolated --network none test container.

No database, environment credentials, public ports or external calls are used.
Tests digest SIP registration and bidirectional PCMU external-media streaming.
"""
import asyncio
import hashlib
import importlib.util
import json
import math
import re
import struct
import wave
from pathlib import Path

import aiohttp
from aiohttp import web


class Registrar(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport
        self.authenticated = False

    def datagram_received(self, data, address):
        request = data.decode(errors='replace')
        if not request.startswith('REGISTER '):
            return
        headers = {}
        for line in request.split('\r\n')[1:]:
            if ': ' in line:
                key, value = line.split(': ', 1)
                headers.setdefault(key.lower(), []).append(value)
        auth = dict(re.findall(r'(\w+)="([^"]*)"', headers.get('authorization', [''])[0]))
        md5 = lambda value: hashlib.md5(value.encode()).hexdigest()
        expected = md5(md5('1001:fixture:fixture-secret') + ':fixture-nonce:' + md5('REGISTER:' + auth.get('uri', '')))
        accepted = auth.get('username') == '1001' and auth.get('nonce') == 'fixture-nonce' and auth.get('response') == expected
        status = '200 OK' if accepted else '401 Unauthorized'
        response = ['SIP/2.0 ' + status]
        for name in ('via', 'from', 'to', 'call-id', 'cseq'):
            for value in headers.get(name, []):
                response.append(name + ': ' + value + (';tag=fixture' if name == 'to' and ';tag=' not in value else ''))
        if accepted:
            response.extend('Contact: ' + value for value in headers.get('contact', []))
            response.append('Expires: 300')
            self.authenticated = True
        else:
            response.append('WWW-Authenticate: Digest realm="fixture",nonce="fixture-nonce",algorithm=MD5')
        response.extend(['Content-Length: 0', '', ''])
        self.transport.sendto('\r\n'.join(response).encode(), address)


async def main():
    path = Path('/app/api/services/telephony/sip_gateway_config.py')
    spec = importlib.util.spec_from_file_location('gateway_config', path)
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    credentials = {'gateway_id': 'a' * 32, 'domain': 'fixture.test:5099', 'resolved_ip': '8.8.8.8', 'username': '1001', 'password': 'fixture-secret', 'number': '+12125550101'}
    generated = config.render_gateway_connections([{'credentials': credentials}])
    directory = Path('/etc/asterisk')
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in generated.items():
        # Only this network-none test substitutes the pinned destination with
        # its local registrar. Production always uses validated public IPv4.
        (directory / name).write_text(content.replace('8.8.8.8:5099', '127.0.0.1:5099'))
    settings = {
        'asterisk.conf': '[directories]\nastetcdir => /etc/asterisk\nastmoddir => /usr/lib/asterisk/modules\nastvarlibdir => /var/lib/asterisk\nastspooldir => /var/spool/asterisk\nastrundir => /var/run/asterisk\nastlogdir => /var/log/asterisk\n[options]\nverbose=0\ndebug=0\n',
        'modules.conf': '[modules]\nautoload=yes\nnoload=res_pjsip_endpoint_identifier_anonymous.so\n',
        'http.conf': '[general]\nenabled=yes\nbindaddr=127.0.0.1\nbindport=8088\n',
        'ari.conf': '[general]\nenabled=yes\n[fixture]\ntype=user\nread_only=no\npassword=fixture-secret\n#include ari_connections.conf\n',
        'extensions.conf': '#include extensions_connections.conf\n',
        'pjsip.conf': '[global]\ntype=global\nendpoint_identifier_order=line,username,auth_username\n[transport-udp]\ntype=transport\nprotocol=udp\nbind=127.0.0.1:5060\n#include pjsip_connections.conf\n',
        'websocket_client.conf': '[sysevo_media]\ntype=websocket_client\nuri=ws://127.0.0.1:9099/media\nprotocols=media\nconnection_type=per_call_config\n',
        'logger.conf': '[general]\n[logfiles]\n',
        'rtp.conf': '[general]\nrtpstart=30000\nrtpend=30020\n',
    }
    for name, content in settings.items():
        (directory / name).write_text(content)
    for name in ('/var/run/asterisk', '/var/log/asterisk', '/var/spool/asterisk/recording', '/var/lib/asterisk'):
        Path(name).mkdir(parents=True, exist_ok=True)
    with wave.open('/tmp/fixture-tone.wav', 'wb') as output:
        output.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        output.writeframes(b''.join(struct.pack('<h', int(10000 * math.sin(2 * math.pi * 440 * i / 8000))) for i in range(8000)))

    registrar = Registrar()
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(lambda: registrar, local_addr=('127.0.0.1', 5099))
    received = bytearray()
    media_started = asyncio.Event()
    media_socket = []

    async def media(request):
        assert request.query.get('probe') == 'fixture', 'ARI transport_data query was lost'
        ws = web.WebSocketResponse(protocols=['media'])
        await ws.prepare(request)
        media_socket.append(ws)
        async for message in ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                if 'MEDIA_START' in message.data:
                    media_started.set()
            elif message.type == aiohttp.WSMsgType.BINARY:
                received.extend(message.data)
        return ws

    app = web.Application()
    app.router.add_get('/media', media)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', 9099).start()
    process = await asyncio.create_subprocess_exec('asterisk', '-f', '-g', stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        async with aiohttp.ClientSession(auth=aiohttp.BasicAuth('fixture', 'fixture-secret'), timeout=aiohttp.ClientTimeout(total=5)) as session:
            base = 'http://127.0.0.1:8088/ari'
            for _ in range(80):
                try:
                    async with session.get(base + '/asterisk/info') as response:
                        if response.status == 200:
                            break
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(.25)
            else:
                raise AssertionError('Asterisk ARI did not start')
            for _ in range(100):
                if registrar.authenticated:
                    break
                await asyncio.sleep(.1)
            assert registrar.authenticated, 'SIP digest registration did not succeed'
            listing = await asyncio.create_subprocess_exec('asterisk', '-rx', 'pjsip show registration sysevo_' + 'a' * 32 + '_registration', stdout=asyncio.subprocess.PIPE)
            status, _ = await listing.communicate()
            assert config.registration_is_ready(status.decode()), 'Registered CLI state was not recognized'
            async with session.ws_connect(base + '/events?app=fixture') as events:
                async with session.post(base + '/channels/externalMedia', params={'app': 'fixture', 'channelId': 'fixture-media', 'external_host': 'sysevo_media', 'format': 'ulaw', 'transport': 'websocket', 'encapsulation': 'none', 'connection_type': 'client', 'direction': 'both', 'transport_data': 'v(probe=fixture)'}) as response:
                    assert response.status == 200, 'externalMedia failed: ' + str(response.status) + ' ' + await response.text()
                await asyncio.wait_for(media_started.wait(), 10)
                async with session.post(base + '/channels/fixture-media/play', params={'media': 'sound:/tmp/fixture-tone'}) as response:
                    assert response.status == 201, 'Audio playback did not start: ' + str(response.status)
                await asyncio.sleep(1.3)
                assert len(received) >= 800 and any(value not in (0x7f, 0xff) for value in received), 'Asterisk sent no playable audio'
                async with session.post(base + '/channels/fixture-media/record', params={'name': 'fixture-record', 'format': 'wav', 'maxDurationSeconds': '2', 'ifExists': 'overwrite', 'beep': 'false'}) as response:
                    assert response.status == 201, 'Recording did not start: ' + str(response.status)
                for _ in range(70):
                    await media_socket[0].send_bytes(bytes([0x9f] * 4 + [0x1f] * 4) * 20)
                    await asyncio.sleep(.02)
                async with session.post(base + '/recordings/live/fixture-record/stop') as response:
                    assert response.status in (204, 404), 'Recording did not stop'
                for _ in range(30):
                    async with session.get(base + '/recordings/stored/fixture-record') as response:
                        if response.status == 200:
                            break
                    await asyncio.sleep(.1)
                record = Path('/var/spool/asterisk/recording/fixture-record.wav')
                assert record.exists(), 'Incoming audio recording was not written'
                with wave.open(str(record), 'rb') as recording:
                    frames = recording.readframes(recording.getnframes())
                assert any(abs(value[0]) > 100 for value in struct.iter_unpack('<h', frames)), 'Asterisk did not receive the injected audio'
                await session.delete(base + '/channels/fixture-media')
        print(json.dumps({'sip_digest_registration': True, 'registered_state_recognized': True, 'transport_data_preserved': True, 'pcmu_audio_to_client_bytes': len(received), 'audio_from_client_recorded': True}))
    finally:
        process.terminate()
        await process.wait()
        transport.close()
        for ws in media_socket:
            await ws.close()
        await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
