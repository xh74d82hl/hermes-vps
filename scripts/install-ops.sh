#!/usr/bin/env bash
# Install the three ops checks: weekly health digest, daily resource guard,
# quarterly restore drill. All root, all independent of the Hermes gateway.
#
#   sudo ./scripts/install-ops.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd /

log() { printf '\n==> %s\n' "$*"; }

log "Scripts"
install -m 700 "$HERE/scripts/notify.sh"        /usr/local/sbin/hermes-notify
install -m 700 "$HERE/scripts/health-digest.sh" /usr/local/sbin/hermes-health-digest
install -m 700 "$HERE/scripts/resource-guard.sh" /usr/local/sbin/hermes-resource-guard
install -m 700 "$HERE/scripts/restore-drill.sh" /usr/local/sbin/hermes-restore-drill
ls -l /usr/local/sbin/hermes-notify /usr/local/sbin/hermes-health-digest \
      /usr/local/sbin/hermes-resource-guard /usr/local/sbin/hermes-restore-drill

log "Units"
install -m 644 "$HERE/systemd/hermes-health.service"       /etc/systemd/system/
install -m 644 "$HERE/systemd/hermes-health.timer"         /etc/systemd/system/
install -m 644 "$HERE/systemd/hermes-guard.service"        /etc/systemd/system/
install -m 644 "$HERE/systemd/hermes-guard.timer"          /etc/systemd/system/
install -m 644 "$HERE/systemd/hermes-drill.service"        /etc/systemd/system/
install -m 644 "$HERE/systemd/hermes-drill.timer"          /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hermes-health.timer hermes-guard.timer hermes-drill.timer

log "Next runs"
systemctl list-timers --all 'hermes-*' --no-pager | sed -n '1,6p'

cat <<'EOF'

Test now (each sends one Telegram message):
  sudo systemctl start hermes-health.service
  sudo systemctl start hermes-resource-guard.service   # silent unless a threshold is crossed
  sudo systemctl start hermes-drill.service
EOF
