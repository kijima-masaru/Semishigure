#!/bin/bash
# Render deploy/asterisk/conf templates (__X__ placeholders) into a runtime conf dir.
# Usage: render-conf.sh <src conf dir> <dst conf dir> <log dir> <run dir> <db dir> <spool dir>
set -euo pipefail
SRC=$1; DST=$2; LOG=$3; RUN=$4; DB=$5; SPOOL=$6
mkdir -p "$DST" "$LOG" "$RUN" "$DB" "$SPOOL"
# asterisk.conf needs absolute paths (asterisk -rx resolves them from its own cwd)
DST=$(readlink -f "$DST"); LOG=$(readlink -f "$LOG"); RUN=$(readlink -f "$RUN"); DB=$(readlink -f "$DB"); SPOOL=$(readlink -f "$SPOOL")
SIP_IP=${SEMI_AST_SIP_IP:-0.0.0.0}
EXTERNAL=""
if [ -n "${SEMI_AST_EXT_IP:-}" ]; then
  EXTERNAL="external_media_address = ${SEMI_AST_EXT_IP}
external_signaling_address = ${SEMI_AST_EXT_IP}"
fi
SEP=${SEMI_AST_RING_SEP:-&}
DIAL="PJSIP/9001${SEP}PJSIP/9002${SEP}PJSIP/9003${SEP}PJSIP/9004"
DIAL_SED=${DIAL//&/\\&}   # '&' is special in sed replacements
for f in "$SRC"/*.conf; do
  sed -e "s#__CONF__#$DST#g" -e "s#__DATADIR__#${SEMI_AST_DATADIR:-/usr/share/asterisk}#g" -e "s#__LOG__#$LOG#g" -e "s#__RUN__#$RUN#g" -e "s#__DB__#$DB#g" -e "s#__SPOOL__#$SPOOL#g" \
      -e "s#__MAXCALLS__#${SEMI_AST_MAXCALLS:-100}#g" \
      -e "s#__SIP_IP__#$SIP_IP#g" -e "s#__SIP_PORT__#${SEMI_AST_SIP_PORT:-5060}#g" \
      -e "s#__DOMAIN__#${SEMI_AST_DOMAIN:-pbx.semishigure.test}#g" \
      -e "s#__EXT_PASSWORD__#${SEMI_AST_EXT_PASSWORD:-semishigure-dev}#g" \
      -e "s#__AMI_PASSWORD__#${SEMI_AST_AMI_PASSWORD:-semishigure-ami}#g" \
      -e "s#__RING_GROUP__#${SEMI_AST_RING_GROUP:-8001}#g" -e "s#__RING_GROUP_LIMIT__#${SEMI_AST_RING_GROUP_LIMIT:-20}#g" \
      -e "s#__RING_GROUP_DIAL__#$DIAL_SED#g" \
      -e "s#__RTP_START__#${SEMI_AST_RTP_START:-16384}#g" -e "s#__RTP_END__#${SEMI_AST_RTP_END:-32768}#g" \
      "$f" > "$DST/$(basename "$f")"
done
python3 - "$DST/pjsip.conf" "$EXTERNAL" <<'PY'
import sys
p, ext = sys.argv[1], sys.argv[2]
s = open(p).read().replace("__EXTERNAL__", ext)
open(p, "w").write(s)
PY
