"""Pure, tenant-separated Asterisk configuration for registered customer trunks."""
import hashlib
import hmac
import re
import ipaddress
from urllib.parse import urlsplit


def gateway_identity(credentials):
    key = credentials.get('gateway_id', '')
    if not re.fullmatch(r'[a-f0-9]{32}', key):
        raise ValueError('SIP gateway connection identity is missing')
    name = 'sysevo_' + key
    password = hmac.new(credentials['password'].encode(), ('sysevo-sip-ari:' + key).encode(), hashlib.sha256).hexdigest()
    return name, password


def asterisk_config(credentials):
    name, password = gateway_identity(credentials)
    return {'provider':'sip', 'ari_endpoint':'http://sysevo-sip-gateway:8088', 'app_name':name, 'app_password':password, 'ws_client_name':'sysevo_media', 'gateway_id':credentials['gateway_id'], 'domain':credentials['domain'], 'username':credentials['username'], 'password':credentials['password'], 'number':credentials['number']}


def escaped(value):
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Invalid Asterisk configuration value')
    return value.replace('\\', '\\\\').replace(';', r'\;')


def render_gateway_connections(rows):
    pjsip=[];ari=[];dialplan=[]
    for row in rows:
        c=row['credentials'];name,password=gateway_identity(c)
        # The loader receives persisted, schema-validated credentials. Validate
        # again at this text boundary so a corrupt row cannot inject a stanza.
        domain=c['domain'];username=c['username'];number=c['number']
        if not re.fullmatch(r'[a-zA-Z0-9.-]+(?::[0-9]{1,5})?',domain) or not re.fullmatch(r'[a-zA-Z0-9+_.-]+',username) or not re.fullmatch(r'\+1[0-9]{10}',number):
            raise ValueError('Invalid stored SIP connection')
        secret=escaped(c['password'])
        address = ipaddress.ip_address(c['resolved_ip'])
        if address.version != 4 or not address.is_global:
            raise ValueError('Invalid stored SIP destination')
        port = urlsplit('sip://' + domain).port or 5060
        proxy = f'sip:{address}:{port}\\;lr'
        pjsip.append(f'''[{name}_auth]
type=auth
auth_type=userpass
username={username}
password={secret}

[{name}_aor]
type=aor
contact=sip:{address}:{port}
qualify_frequency=30

[{name}]
type=endpoint
transport=transport-udp
context={name}
disallow=all
allow=ulaw
outbound_auth={name}_auth
outbound_proxy={proxy}
aors={name}_aor
from_user={username}
from_domain={urlsplit('sip://'+domain).hostname}
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes

[{name}_registration]
type=registration
transport=transport-udp
outbound_auth={name}_auth
server_uri=sip:{domain}
outbound_proxy={proxy}
client_uri=sip:{username}@{domain}
contact_user={number}
retry_interval=60
forbidden_retry_interval=600
max_retries=3
expiration=300
max_random_initial_delay=0
line=yes
endpoint={name}
''')
        ari.append(f'[{name}]\ntype=user\nread_only=no\npassword={password}\n')
        # Every incoming trunk gets its own Stasis application and fixed called
        # number. No catch-all context can enter another tenant's application.
        dialplan.append(f'[{name}]\nexten => _.,1,Set(CALLERID(dnid)={number})\nsame => n,Goto({name}_agent,{number},1)\n[{name}_agent]\nexten => {number},1,Stasis({name})\nsame => n,Hangup()\n')
    return {'pjsip_connections.conf':'\n'.join(pjsip),'ari_connections.conf':'\n'.join(ari),'extensions_connections.conf':'\n'.join(dialplan)}


def registration_is_ready(output):
    # Read the exact state token, not a substring of "Unregistered".
    return bool(re.search(r'(?mi)^\s*Status\s*:\s*Registered\s*$',output) or re.search(r'(?m)^\s*sysevo_[a-f0-9]{32}_registration/\S+\s+\S+\s+Registered\b',output))
