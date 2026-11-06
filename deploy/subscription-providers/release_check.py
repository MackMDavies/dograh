"""Read-only release preflight. Resolved Compose secrets never leave memory."""
import copy
import json
import os
import subprocess
import sys
from pathlib import Path


def output(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE)


def container(name):
    return json.loads(output('docker', 'inspect', name))[0]


def image(name):
    return json.loads(output('docker', 'image', 'inspect', name))[0]


root = Path(sys.argv[1]).resolve()
api = container('dograh-api-1')
worker = container('dograh-worker-1')
assert api['Image'] == 'sha256:6972b6e7305b704eae47793cb106b914ae681e2635f974fbce653804337ea638', 'API base changed; rebuild first'
assert worker['Image'] == 'sha256:3ad111549060dc2d73feb55c1af169cd45abff4205d7e625da079f2f4e092a67', 'Worker base changed; rebuild first'
images = {name: 'dograh-local/subscription-providers-' + name + ':74180fe' for name in ('api', 'worker', 'gateway')}
for name, running in (('api', api), ('worker', worker), ('gateway', api)):
    parent = image(running['Image'])['RootFS']['Layers']
    candidate = image(images[name])['RootFS']['Layers']
    assert candidate[:len(parent)] == parent, name + ' does not preserve all running image layers'

labels = api['Config']['Labels']
working = labels['com.docker.compose.project.working_dir']
files = labels['com.docker.compose.project.config_files'].split(',')
assert all(Path(path).is_file() for path in files), 'An active Compose file is missing'
command = ['docker', 'compose', '--project-directory', working, '--project-name', labels['com.docker.compose.project']]
for path in files:
    command += ['-f', path]
before = json.loads(output(*command, 'config', '--format', 'json'))
overlay = root / 'deploy/subscription-providers/compose.release.yaml'
overlay.write_text('services:\n  api:\n    image: ' + images['api'] + '\n    environment:\n      SYSEVO_SIP_GATEWAY_ENABLED: "true"\n  worker:\n    image: ' + images['worker'] + '\n    environment:\n      SYSEVO_SIP_GATEWAY_ENABLED: "true"\n')
environment = {**os.environ, 'SYSEVO_SIP_GATEWAY_IMAGE': images['gateway'], 'SYSEVO_SIP_GATEWAY_ENABLED': 'true', 'SYSEVO_SIP_PUBLIC_IP': '178.105.240.245'}
candidate_command = command + ['-f', str(overlay), '-f', str(root / 'deploy/sip-gateway/compose.yaml'), '--profile', 'subscription-sip']
after = json.loads(subprocess.check_output(candidate_command + ['config', '--format', 'json'], env=environment, text=True, stderr=subprocess.PIPE))
for service, current in before['services'].items():
    expected = copy.deepcopy(current)
    if service in ('api', 'worker'):
        expected['image'] = images[service]
        expected.setdefault('environment', {})['SYSEVO_SIP_GATEWAY_ENABLED'] = 'true'
    assert expected == after['services'][service], 'Unexpected effective Compose change in ' + service

for name, running in (('api', api), ('worker', worker)):
    active_env = dict(value.split('=', 1) for value in running['Config']['Env'])
    drift = [key for key, value in before['services'][name].get('environment', {}).items() if str(value) != active_env.get(key)]
    assert not drift, 'Existing runtime/environment drift in ' + name + ': ' + ', '.join(drift)
    assert not any(volume.get('target') == '/app/api' for volume in after['services'][name].get('volumes', [])), 'A source bind mount would hide the release code'

gateway = after['services']['sysevo-sip-gateway']
active_env = dict(value.split('=', 1) for value in api['Config']['Env'])
for key in ('DATABASE_URL', 'REDIS_URL', 'OSS_JWT_SECRET', 'SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY', 'SYSEVO_PRE_CALL_CHECK_URL', 'SYSEVO_MEMORY_SECRET'):
    assert gateway['environment'].get(key) == active_env.get(key) and active_env.get(key), 'Gateway runtime setting differs: ' + key
assert all(str(port.get('target')) not in ('8088', '8089') for port in gateway.get('ports', [])), 'Gateway control ports must stay private'
assert gateway['read_only'] is True and gateway['cap_drop'] == ['ALL']
print(json.dumps({'preserved_running_image_layers': True, 'preserved_existing_compose_services': True, 'environment_drift': False, 'gateway_tenant_and_billing_context': True, 'private_gateway_control_ports': True, 'compose_file_count': len(files), 'candidate_images': images}))
