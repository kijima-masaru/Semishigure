#!/bin/bash
# Run a locally built FreeSWITCH (prefix /usr/local/freeswitch) with the Semishigure dev conf.
# Usage: SEMI_FS_SIP_IP=127.0.0.1 ./run-native.sh [-nf]
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
PREFIX=${FS_PREFIX:-/usr/local/freeswitch}
mkdir -p "$HERE/log" "$HERE/db" "$HERE/certs"
export SEMI_FS_CERTS_DIR=${SEMI_FS_CERTS_DIR:-$HERE/certs}
"$HERE/gen-certs.sh" "$SEMI_FS_CERTS_DIR"
exec "$PREFIX/bin/freeswitch" -nonat -nc -nf \
  -conf "$HERE/conf" -log "$HERE/log" -db "$HERE/db" -run "$HERE/log" \
  -mod "$PREFIX/lib/freeswitch/mod" "$@"
