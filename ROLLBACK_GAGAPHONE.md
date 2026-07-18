# Hermes GagaPhone Integration Rollback

## Backup Locations

Primary backup directory:

`/root/.hermes/backups/unified-alarm-backup-20260718-124049`

Remote backup file:

`/root/.hermes/backups/unified-alarm-backup-20260718-124049/git/remotes-before-push.txt`

## Manual Rollback Steps

1. Stop integration services:

```bash
systemctl --user disable --now gagaphone-hermes-bridge.service
systemctl disable --now gagaphone-api-tunnel.service
```

2. Remove runtime plugin and bridge:

```bash
rm -rf /root/.hermes/plugins/gagaphone
rm -f /root/.hermes/gagaphone_bridge.py
rm -f /root/.config/systemd/user/gagaphone-hermes-bridge.service
rm -f /etc/systemd/system/gagaphone-api-tunnel.service
systemctl --user daemon-reload
systemctl daemon-reload
```

3. Restore Hermes config/environment from backup if desired:

```bash
cp /root/.hermes/backups/unified-alarm-backup-20260718-124049/config.yaml /root/.hermes/config.yaml
cp /root/.hermes/backups/unified-alarm-backup-20260718-124049/.env /root/.hermes/.env
chmod 600 /root/.hermes/config.yaml /root/.hermes/.env
```

4. Remove tunnel account if no longer used:

```bash
userdel -r gagapipe
```

5. Restore Hermes repository branch if desired:

```bash
cd /usr/local/lib/hermes-agent
git reset --hard d2c81eb681dea1382fbd1ed403f58320d5aef575
```

Only run `git reset --hard` when you have confirmed no user changes need preserving.

6. Restart Hermes Gateway:

```bash
systemctl --user restart hermes-gateway.service
systemctl --user status hermes-gateway.service --no-pager
```

## Checked Rollback Script

A helper script is provided at `deploy/gagaphone/rollback_gagaphone_integration.sh`. It is not executed automatically.
