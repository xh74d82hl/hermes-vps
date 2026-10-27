#!/usr/bin/env bash
# Install nightly encrypted restic backup of the agent's Hermes data to Backblaze B2.
# Needs /etc/hermes-backup.env (root:root 0600), created from backup.example.env.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$HERE/vars.env"
[ -f /etc/hermes-backup.env ] || { echo "create /etc/hermes-backup.env first (see backup.example.env)" >&2; exit 1; }
chmod 600 /etc/hermes-backup.env

cat > /usr/local/sbin/hermes-backup <<EOF
#!/usr/bin/env bash
set -euo pipefail
set -a; . /etc/hermes-backup.env; set +a
SRC="/home/$AGENT_USER/.hermes"
DUMP=/var/backups/hermes-dbs
mkdir -p "\$DUMP"; chmod 700 "\$DUMP"
# Consistent copies of live SQLite DBs (WAL-safe).
# NOTE: the copy gets a .dump suffix on purpose — the *.db exclude below would otherwise
# drop the dumps too, and you would back up the config but not the actual memory/state.
find "\$SRC" -name '*.db' -not -path '*/hermes-agent/*' 2>/dev/null | while read -r db; do
  out="\$DUMP/\$(echo "\${db#\$SRC/}" | tr '/' '_').dump"
  sqlite3 "\$db" ".backup '\$out'"
done
restic backup \${RESTIC_EXTRA_ARGS:-} --tag hermes \\
  --exclude "\$SRC/hermes-agent" --exclude "\$SRC/tools" --exclude "\$SRC/installs" \\
  --exclude "\$SRC/logs" --exclude "\$SRC/cache" --exclude "\$SRC/image_cache" \\
  --exclude "\$SRC/audio_cache" \\
  --exclude '*.db' --exclude '*.db-wal' --exclude '*.db-shm' \\
  "\$SRC" "\$DUMP" \$(for d in ${BACKUP_EXTRA_PATHS:-}; do [ -e "\$d" ] && echo "\$d"; done)
EOF
chmod 700 /usr/local/sbin/hermes-backup

cp "$HERE/systemd/hermes-backup.service" "$HERE/systemd/hermes-backup.timer" /etc/systemd/system/
systemctl daemon-reload
set -a; . /etc/hermes-backup.env; set +a
restic snapshots >/dev/null 2>&1 || restic init
systemctl enable --now hermes-backup.timer
echo "Test now: sudo systemctl start hermes-backup.service && sudo restic -r \$RESTIC_REPOSITORY snapshots"
echo "SAVE RESTIC_PASSWORD in your password manager: without it the backup is unrecoverable."
