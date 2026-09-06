#!/bin/bash
# Generate a self-signed certificate for the dev PBX's SIP-TLS listener (FreeSWITCH layout:
# agent.pem = cert + key, cafile.pem = cert). Skipped when agent.pem already exists.
set -euo pipefail
DIR=${1:?certs dir}
mkdir -p "$DIR"
[ -f "$DIR/agent.pem" ] && exit 0
CN=${SEMI_FS_DOMAIN:-pbx.semishigure.test}
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$CN" \
  -addext "subjectAltName=DNS:$CN,DNS:localhost,IP:127.0.0.1" \
  -keyout "$DIR/agent.key" -out "$DIR/agent.crt" >/dev/null 2>&1
cat "$DIR/agent.crt" "$DIR/agent.key" > "$DIR/agent.pem"
cp "$DIR/agent.crt" "$DIR/cafile.pem"
chmod 600 "$DIR/agent.pem" "$DIR/agent.key"
echo "generated self-signed certificate in $DIR (CN=$CN)"
