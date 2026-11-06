"""Run the committed compatibility probe without exposing runtime credentials.

Uses Docker's local socket so credentials never enter argv, files, or stdout.
The probe container has no network and executes no DB queries or migrations.
"""
import http.client
import json
import socket
import sys
from urllib.parse import quote, urlsplit


class Docker(http.client.HTTPConnection):
    def __init__(self):
        super().__init__('localhost', timeout=120)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect('/var/run/docker.sock')


def request(method, path, body=None, raw=False):
    client = Docker()
    client.request(method, path, body=None if body is None else json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    response = client.getresponse()
    result = response.read()
    status = response.status
    client.close()
    if status >= 400:
        raise RuntimeError('Docker request failed: ' + str(status))
    return result if raw else json.loads(result or b'{}')


image, probe_path, mode = sys.argv[1:]
runtime = request('GET', '/containers/dograh-api-1/json')
environment = dict(item.split('=', 1) for item in runtime['Config']['Env'])
keys = ('DATABASE_URL', 'REDIS_URL', 'OSS_JWT_SECRET')
assert all(environment.get(key) for key in keys), 'Required runtime settings are missing'
name = 'sysevo-provider-runtime-probe-' + mode
created = request('POST', '/containers/create?name=' + quote(name), {
    'Image': image, 'Entrypoint': ['python'], 'Cmd': ['/probe.py'] + (['--api'] if mode == 'api' else []),
    'Env': [key + '=' + environment[key] for key in keys] + ['LOG_LEVEL=CRITICAL', 'LOG_TO_FILE=false', 'PYTHONDONTWRITEBYTECODE=1'],
    'HostConfig': {'NetworkMode': 'none', 'Memory': 1610612736, 'NanoCpus': 1000000000,
                   'ReadonlyRootfs': True, 'Binds': [probe_path + ':/probe.py:ro'],
                   'Tmpfs': {'/tmp': 'rw,noexec,nosuid,size=16m', '/app/dograh_pcm_cache': 'rw,noexec,nosuid,size=8m,mode=1777'}},
})
identifier = created['Id']
try:
    request('POST', '/containers/' + identifier + '/start')
    completed = request('POST', '/containers/' + identifier + '/wait?condition=not-running')
    output = request('GET', '/containers/' + identifier + '/logs?stdout=1&stderr=1', raw=True)
    # Docker non-TTY logs use an eight-byte frame header.
    frames = []
    while len(output) >= 8:
        length = int.from_bytes(output[4:8], 'big')
        frames.append(output[8:8 + length].decode(errors='replace'))
        output = output[8 + length:]
    text = ''.join(frames)
    for key in keys:
        value = environment[key]
        text = text.replace(value, '[redacted]')
        if key.endswith('URL'):
            password = urlsplit(value).password
            if password:
                text = text.replace(password, '[redacted]')
    print(text)
    if completed.get('StatusCode') != 0:
        raise SystemExit(1)
finally:
    request('DELETE', '/containers/' + identifier + '?force=1&v=1')
