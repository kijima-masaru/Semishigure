#!/bin/bash
# Renders the templates with SEMI_AST_* environment variables and starts Asterisk in the foreground.
# SEMI_AST_SSH=1 also starts sshd (port 2222, user 'semi', key from SEMI_AST_SSH_PUBKEY) for the SshExecutor.
set -euo pipefail
render-conf.sh /etc/semishigure-ast/templates /etc/asterisk /var/log/asterisk /var/run/asterisk /var/lib/asterisk/astdb /var/spool/asterisk
if [ "${SEMI_AST_SSH:-0}" = "1" ]; then
  id semi >/dev/null 2>&1 || useradd -m -s /bin/bash semi
  mkdir -p /home/semi/.ssh && chmod 700 /home/semi/.ssh
  if [ -n "${SEMI_AST_SSH_PUBKEY:-}" ]; then
    echo "$SEMI_AST_SSH_PUBKEY" > /home/semi/.ssh/authorized_keys
    chmod 600 /home/semi/.ssh/authorized_keys
  fi
  chown -R semi:semi /home/semi/.ssh
  chmod -R a+rX /var/log/asterisk
  /usr/sbin/sshd -p "${SEMI_AST_SSH_PORT:-2222}"
fi
exec asterisk -f -C /etc/asterisk/asterisk.conf "$@"
