#!/usr/bin/env bash
# Idempotent base setup for a Hermes agent VPS (tested on Debian 13; Debian 12 / Ubuntu 22.04+ OK).
# Run as root, once, over SSH.
# Does NOT lock root/password SSH yet: run scripts/harden-ssh.sh after verifying admin login.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
[ -f "$HERE/vars.env" ] || { echo "missing vars.env (copy vars.example.env)" >&2; exit 1; }
# shellcheck disable=SC1091
. "$HERE/vars.env"
: "${ADMIN_USER:?}" "${ADMIN_SSH_PUBKEY:?}" "${AGENT_USER:?}"
ADMIN_SSH_PUBKEY="$(printf '%s' "$ADMIN_SSH_PUBKEY" | tr -d '\r\n')"  # tolerate pasted line breaks
SWAP_GB="${SWAP_GB:-2}"; TIMEZONE="${TIMEZONE:-UTC}"; INSTALL_DOCKER="${INSTALL_DOCKER:-no}"

export DEBIAN_FRONTEND=noninteractive
log() { printf '\n==> %s\n' "$*"; }

log "Packages"
apt-get update -y
apt-get install -y --no-install-recommends \
  ca-certificates curl git tar xz-utils gnupg sudo ufw fail2ban unattended-upgrades \
  libatomic1 python3-systemd restic sqlite3 jq htop tmux rsync
# snapd is useless here and costs RAM; ignore if absent.
apt-get purge -y snapd 2>/dev/null || true
if [ "$INSTALL_DOCKER" = yes ]; then apt-get install -y --no-install-recommends docker.io; fi
timedatectl set-timezone "$TIMEZONE" || true

log "Admin user ($ADMIN_USER)"
if ! id "$ADMIN_USER" >/dev/null 2>&1; then adduser --disabled-password --gecos "" "$ADMIN_USER"; fi
usermod -aG sudo "$ADMIN_USER"
install -d -m 700 -o "$ADMIN_USER" -g "$ADMIN_USER" "/home/$ADMIN_USER/.ssh"
AK="/home/$ADMIN_USER/.ssh/authorized_keys"
touch "$AK"; grep -qxF "$ADMIN_SSH_PUBKEY" "$AK" || echo "$ADMIN_SSH_PUBKEY" >> "$AK"
chown "$ADMIN_USER:$ADMIN_USER" "$AK"; chmod 600 "$AK"
# Passwordless sudo is convenient but only the admin (never the agent) gets it.
echo "$ADMIN_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-$ADMIN_USER"; chmod 440 "/etc/sudoers.d/90-$ADMIN_USER"

log "Agent user ($AGENT_USER): no sudo, no ssh, no docker group"
if ! id "$AGENT_USER" >/dev/null 2>&1; then adduser --disabled-password --gecos "" "$AGENT_USER"; fi
passwd -l "$AGENT_USER" >/dev/null || true
loginctl enable-linger "$AGENT_USER"   # user services survive logout/reboot

log "Swap (${SWAP_GB}G)"
if ! swapon --show | grep -q /swapfile; then
  [ -f /swapfile ] || { fallocate -l "${SWAP_GB}G" /swapfile; chmod 600 /swapfile; mkswap /swapfile; }
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
echo 'vm.swappiness=10' > /etc/sysctl.d/99-swappiness.conf; sysctl -p /etc/sysctl.d/99-swappiness.conf >/dev/null

log "Firewall: deny in, allow SSH"
ufw --force reset >/dev/null
ufw default deny incoming; ufw default allow outgoing
ufw allow 22/tcp
ufw --force enable

log "fail2ban + unattended-upgrades"
cat > /etc/fail2ban/jail.d/sshd.local <<'EOF'
[sshd]
enabled = true
backend = systemd
maxretry = 5
bantime = 1h
EOF
systemctl enable --now fail2ban; systemctl restart fail2ban
dpkg-reconfigure -f noninteractive unattended-upgrades

log "Done. Next:"
cat <<EOF
 1. In a NEW terminal:  ssh $ADMIN_USER@<IP>   (must work, key-only)
 2. Then on the server: sudo $HERE/scripts/harden-ssh.sh
 3. Install Hermes as the agent user:  sudo $HERE/scripts/install-hermes.sh
 4. Backups: see README (scripts/setup-backup.sh)
EOF
