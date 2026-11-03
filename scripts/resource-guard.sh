#!/usr/bin/env bash
# Daily thresholds. Silent unless something is actually wrong, so it never becomes noise.
set -uo pipefail

AGENT_USER="hermes"
AGENT_HOME="/home/$AGENT_USER"
ENVF="/etc/hermes-backup.env"
RSS_LIMIT_MB=700
DISK_LIMIT=85
MEM_LIMIT_MB=250
TOKEN_AGE_LIMIT_D=6
msgs=()

as_agent() { sudo -u "$AGENT_USER" env HOME="$AGENT_HOME" XDG_RUNTIME_DIR="/run/user/$(id -u "$AGENT_USER")" \
             DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u "$AGENT_USER")/bus" bash -lc "$1"; }

DISK="$(df -P / | awk 'NR==2 {gsub("%","",$5); print $5}')"
[ "${DISK:-0}" -ge "$DISK_LIMIT" ] && msgs+=("Disco al ${DISK}% (soglia ${DISK_LIMIT}%)")

MEM="$(free -m | awk '/^Mem:/ {print $7}')"
[ "${MEM:-9999}" -le "$MEM_LIMIT_MB" ] && msgs+=("Memoria disponibile ${MEM} MB (soglia ${MEM_LIMIT_MB})")

RSS="$(as_agent 'systemctl --user show hermes-gateway -p MemoryCurrent --value' 2>/dev/null)"
case "$RSS" in
  ''|*[!0-9]*) ;;
  *) [ "$RSS" -ge $((RSS_LIMIT_MB*1024*1024)) ] && msgs+=("Gateway a $((RSS/1048576)) MB (soglia ${RSS_LIMIT_MB})") ;;
esac

ACTIVE="$(as_agent 'systemctl --user is-active hermes-gateway' 2>/dev/null)"
[ "$ACTIVE" = "active" ] || msgs+=("Gateway non attivo: ${ACTIVE:-sconosciuto}")

if [ -r "$ENVF" ] && command -v restic >/dev/null 2>&1; then
  AGE="$(set -a; . "$ENVF"; set +a; HOME=/root restic snapshots --latest 1 --json 2>/dev/null | python3 -c '
import sys, json, datetime
try: s = json.load(sys.stdin)
except Exception: raise SystemExit(0)
if not s: raise SystemExit(0)
t = datetime.datetime.fromisoformat(s[-1]["time"].replace("Z", "+00:00"))
print(int((datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() // 3600))
' 2>/dev/null)"
  case "${AGE:-}" in
    ''|*[!0-9]*) msgs+=("Backup: non riesco a leggere l'ultimo snapshot") ;;
    *) [ "$AGE" -gt 26 ] && msgs+=("Backup fermo da ${AGE}h (soglia 26h)") ;;
  esac
fi

TOK="$AGENT_HOME/.hermes/google_token.json"
if [ -f "$TOK" ]; then
  AGE_D=$(( ( $(date +%s) - $(stat -c %Y "$TOK") ) / 86400 ))
  [ "$AGE_D" -ge "$TOKEN_AGE_LIMIT_D" ] && \
    msgs+=("Token Google aggiornato ${AGE_D} giorni fa: se l'app OAuth è in Testing scade al settimo")
fi

if [ "${#msgs[@]}" -gt 0 ]; then
  printf 'Guardia Hermes\n\n' | cat - <(printf -- '- %s\n' "${msgs[@]}") | /usr/local/sbin/hermes-notify
fi
