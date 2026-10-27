#!/usr/bin/env bash
# Install the Telegram failure-alert channel: a dedicated bot, called by systemd's
# OnFailure= when a unit dies. No dependency on the Hermes gateway.
#
# Run as root:  sudo ./scripts/install-alerts.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '\n==> %s\n' "$*"; }

log "Sender"
install -m 700 "$HERE/scripts/alert-telegram.sh" /usr/local/sbin/hermes-alert
echo "/usr/local/sbin/hermes-alert"

log "Template unit"
install -m 644 "$HERE/systemd/hermes-alert@.service" /etc/systemd/system/hermes-alert@.service

log "Hook the backup unit (drop-in, keeps the repo unit generic)"
install -d -m 755 /etc/systemd/system/hermes-backup.service.d
cat > /etc/systemd/system/hermes-backup.service.d/onfailure.conf <<'EOF'
[Unit]
OnFailure=hermes-alert@%n.service
EOF
cat /etc/systemd/system/hermes-backup.service.d/onfailure.conf

log "Credentials"
if [ -f /etc/hermes-alert.env ]; then
  echo "kept existing /etc/hermes-alert.env"
else
  install -m 600 "$HERE/alert.example.env" /etc/hermes-alert.env
  chown root:root /etc/hermes-alert.env
  echo "created /etc/hermes-alert.env from the example"
fi
ls -l /etc/hermes-alert.env

systemctl daemon-reload

cat <<EOF

Next:
 1. sudo nano /etc/hermes-alert.env      # bot token + your chat id
 2. send a test alert:  sudo hermes-alert hermes-backup.service
 3. test the real path: sudo systemd-run --unit=alert-test \\
       --property=OnFailure=hermes-alert@%n.service /bin/false
EOF
