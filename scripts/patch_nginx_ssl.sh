#!/bin/bash
# Patches the dograh-init generated nginx config:
# 1. Removes the proxy to the Dograh UI (replaced by Vercel-hosted Sysevo SPA)
# 2. Adds production TLS cipher settings
set -euo pipefail

CONF="/var/lib/docker/volumes/dograh_nginx-generated/_data/default.conf"

# Wait for dograh-init to write the config (up to 30s)
for i in 1 2 3 4 5 6 7 8 9 10; do
  [ -f "$CONF" ] && grep -q 'ssl_prefer_server_ciphers' "$CONF" && break
  sleep 3
done

python3 - "$CONF" << 'PY'
import sys, re

path = sys.argv[1]
conf = open(path).read()

# 1. Remove ui upstream and replace / location with 404 (UI is on Vercel)
conf = re.sub(
    r'location\s*/\s*\{[^}]*proxy_pass\s+http://ui[^}]*\}',
    'location / {\n        return 404;\n    }',
    conf,
    flags=re.DOTALL
)
# Remove sub_filter lines that referenced ui
conf = re.sub(r'\s+sub_filter[^\n]*\n', '\n', conf)
conf = re.sub(r'\s+sub_filter_[^\n]*\n', '\n', conf)

# 2. Add TLS session settings (idempotent)
if 'ssl_session_cache' not in conf:
    patch = (
        "ssl_prefer_server_ciphers on;\n"
        "    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256"
        ":ECDHE-RSA-AES256-GCM-SHA384:ECDHE-RSA-CHACHA20-POLY1305;\n"
        "    ssl_session_cache shared:SSL:10m;\n"
        "    ssl_session_timeout 1d;\n"
        "    ssl_session_tickets off;"
    )
    conf = conf.replace("ssl_prefer_server_ciphers on;", patch, 1)

open(path, "w").write(conf)
print("nginx config patched")
PY

docker exec nginx_https nginx -t 2>&1 && docker exec nginx_https nginx -s reload
echo "nginx reloaded"
