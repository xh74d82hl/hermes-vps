#!/usr/bin/env bash
# Disable root login and password auth. Run ONLY after confirming key login as ADMIN_USER works.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$HERE/vars.env"
AK="/home/$ADMIN_USER/.ssh/authorized_keys"
[ -s "$AK" ] || { echo "no authorized_keys for $ADMIN_USER, refusing" >&2; exit 1; }

# 00- prefix: sshd uses the FIRST value seen, so this beats cloud-init's 50-cloud-init.conf.
cat > /etc/ssh/sshd_config.d/00-hardening.conf <<EOF
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AllowUsers $ADMIN_USER
MaxAuthTries 3
X11Forwarding no
EOF
sshd -t
systemctl reload ssh 2>/dev/null || systemctl reload sshd
echo "Hardened. KEEP this session open and test in another terminal: ssh $ADMIN_USER@<IP>"
echo "Verify: ssh -o PubkeyAuthentication=no root@<IP>  must be refused."
