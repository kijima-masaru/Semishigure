#!/bin/bash
# Starts FreeSWITCH in the foreground with the Semishigure dev configuration.
# Optional: SEMI_FS_SSH=1 starts sshd (stage 3, SshExecutor tests) with the
# public key from SEMI_FS_SSH_PUBKEY for user 'semi'.
set -euo pipefail
if [ "${SEMI_FS_SSH:-0}" = "1" ]; then
  id semi >/dev/null 2>&1 || useradd -m -s /bin/bash semi
  mkdir -p /home/semi/.ssh && chmod 700 /home/semi/.ssh
  if [ -n "${SEMI_FS_SSH_PUBKEY:-}" ]; then
    echo "$SEMI_FS_SSH_PUBKEY" > /home/semi/.ssh/authorized_keys
    chmod 600 /home/semi/.ssh/authorized_keys
  fi
  chown -R semi:semi /home/semi/.ssh
  chmod -R a+rX /var/log/freeswitch
  mkdir -p /run/sshd
  /usr/sbin/sshd -p "${SEMI_FS_SSH_PORT:-2222}"
fi
/usr/local/bin/gen-certs.sh "${SEMI_FS_CERTS_DIR:-/etc/semishigure-fs/certs}"
exec /usr/local/freeswitch/bin/freeswitch -nonat -nc -nf \
  -conf /etc/semishigure-fs -log /var/log/freeswitch -db /var/lib/freeswitch/db -run /var/run/freeswitch \
  -mod /usr/local/freeswitch/lib/freeswitch/mod "$@"
