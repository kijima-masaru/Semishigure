#!/bin/bash
# Run the distro's Asterisk (apt install asterisk) with the Semishigure dev conf in the foreground.
# Usage: SEMI_AST_SIP_IP=127.0.0.1 SEMI_AST_SIP_PORT=5090 ./run-native.sh
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
RT=$HERE/runtime
"$HERE/render-conf.sh" "$HERE/conf" "$RT/etc" "$HERE/log" "$RT/run" "$RT/db" "$RT/spool"
exec asterisk -f -C "$RT/etc/asterisk.conf" "$@"
