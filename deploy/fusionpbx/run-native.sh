#!/usr/bin/env bash
# Start the FusionPBX web UI (PHP built-in server) and the FreeSWITCH that FusionPBX manages.
# Usage: sudo deploy/fusionpbx/run-native.sh [freeswitch prefix]   (default /usr/local/freeswitch)
set -euo pipefail
FS_PREFIX=${1:-/usr/local/freeswitch}
WEB_PORT=${FUSIONPBX_WEB_PORT:-8090}
if ! pgrep -f "php -S 127.0.0.1:$WEB_PORT" >/dev/null; then
  (cd /var/www/fusionpbx && nohup php -S 127.0.0.1:$WEB_PORT -t /var/www/fusionpbx router.php >/var/log/fusionpbx-web.log 2>&1 &)
  echo "FusionPBX web: http://127.0.0.1:$WEB_PORT/"
fi
if pgrep -x freeswitch >/dev/null; then
  echo "freeswitch is already running (pid $(pgrep -x freeswitch | head -1))"; exit 0
fi
"$FS_PREFIX/bin/freeswitch" -nonat -nc -nf -conf /etc/freeswitch -log /var/log/freeswitch -db /var/lib/freeswitch/db \
  -run /var/run/freeswitch -scripts /usr/share/freeswitch/scripts -mod "$FS_PREFIX/lib/freeswitch/mod" >/var/log/freeswitch/stdout.log 2>&1 &
for _ in $(seq 1 30); do
  if "$FS_PREFIX/bin/fs_cli" -p ClueCon -x "sofia status" 2>/dev/null | grep -q "internal.*RUNNING"; then
    "$FS_PREFIX/bin/fs_cli" -p ClueCon -x "sofia status"; exit 0
  fi
  sleep 1
done
echo "freeswitch did not come up; see /var/log/freeswitch/freeswitch.log" >&2; exit 1
