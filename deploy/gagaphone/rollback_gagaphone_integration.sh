#!/usr/bin/env bash
set -euo pipefail
BACKUP_DIR="/root/.hermes/backups/unified-alarm-backup-20260718-124049"
PRE_COMMIT="d2c81eb681dea1382fbd1ed403f58320d5aef575"

printf 'This will disable Hermes GagaPhone integration and restore config backups. Type ROLLBACK to continue: '
read -r confirm
if [[ "$confirm" != "ROLLBACK" ]]; then
  echo "aborted"
  exit 1
fi

systemctl --user disable --now gagaphone-hermes-bridge.service || true
systemctl disable --now gagaphone-api-tunnel.service || true
rm -rf /root/.hermes/plugins/gagaphone
rm -f /root/.hermes/gagaphone_bridge.py
rm -f /root/.config/systemd/user/gagaphone-hermes-bridge.service
rm -f /etc/systemd/system/gagaphone-api-tunnel.service
if [[ -f "$BACKUP_DIR/config.yaml" ]]; then cp "$BACKUP_DIR/config.yaml" /root/.hermes/config.yaml; fi
if [[ -f "$BACKUP_DIR/.env" ]]; then cp "$BACKUP_DIR/.env" /root/.hermes/.env; chmod 600 /root/.hermes/.env; fi
systemctl --user daemon-reload || true
systemctl daemon-reload || true
cd /usr/local/lib/hermes-agent
git reset --hard "$PRE_COMMIT"
systemctl --user restart hermes-gateway.service
systemctl --user status hermes-gateway.service --no-pager
