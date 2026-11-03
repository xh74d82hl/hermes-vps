#!/usr/bin/env bash
# Weekly health digest: the things that fail silently.
# Run by hermes-health.timer (root). Prints nothing to the user directly, it sends
# one Telegram message through notify.sh, which does not depend on the gateway.
set -uo pipefail

AGENT_USER="hermes"
AGENT_HOME="/home/$AGENT_USER"
ENVF="/etc/hermes-backup.env"
lines=()
fail=0
warn=0

as_agent() { sudo -u "$AGENT_USER" env HOME="$AGENT_HOME" XDG_RUNTIME_DIR="/run/user/$(id -u "$AGENT_USER")" \
             DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u "$AGENT_USER")/bus" bash -lc "$1"; }

add() { lines+=("$1"); }

# --- doctor.sh: failures and warnings only
DOC="$(bash /opt/hermes-vps/scripts/doctor.sh 2>&1 || true)"
FAILS="$(printf '%s\n' "$DOC" | grep -E '^\s+(FAIL|WARN)' || true)"
if [ -n "$FAILS" ]; then
  add "doctor:"
  while IFS= read -r l; do add "  $l"; done <<< "$FAILS"
  fail=$(printf '%s\n' "$FAILS" | grep -c 'FAIL' || true)
  warn=$(printf '%s\n' "$FAILS" | grep -c 'WARN' || true)
else
  add "doctor: tutto ok"
fi

# --- cron: jobs that are late, failing, or unable to deliver
CRON="$(as_agent 'hermes cron doctor' 2>&1 || true)"
if printf '%s\n' "$CRON" | grep -qiE 'issue|error|failed|late|overdue'; then
  add "cron: ci sono problemi"
  printf '%s\n' "$CRON" | grep -iE 'issue|error|failed|late|overdue' | head -6 | while IFS= read -r l; do add "  ${l:0:110}"; done
  fail=$((fail+1))
else
  add "cron: nessun problema"
fi

# --- backup: age of the newest snapshot and next scheduled run
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
    ''|*[!0-9]*) add "backup: non riesco a leggere l'ultimo snapshot"; fail=$((fail+1)) ;;
    *) if [ "$AGE" -le 36 ]; then add "backup: ultimo snapshot ${AGE}h fa"
       else add "backup: ultimo snapshot ${AGE}h fa, troppo vecchio"; fail=$((fail+1)); fi ;;
  esac
  NEXT="$(systemctl list-timers hermes-backup.timer --no-pager 2>/dev/null | sed -n '2p' | awk '{print $1, $2, $3}')"
  [ -n "$NEXT" ] && add "backup: prossimo giro $NEXT"
else
  add "backup: configurazione non leggibile"; warn=$((warn+1))
fi

# --- host: disk, memory, gateway
DISK="$(df -P / | awk 'NR==2 {gsub("%","",$5); print $5}')"
AVAIL="$(df -h / | awk 'NR==2 {print $4}')"
add "disco: ${DISK}% usato, ${AVAIL} liberi"
[ "${DISK:-0}" -ge 85 ] && fail=$((fail+1)) && add "  ^ oltre la soglia"

MEM="$(free -m | awk '/^Mem:/ {print $7}')"
add "memoria: ${MEM} MB disponibili"
[ "${MEM:-9999}" -le 250 ] && warn=$((warn+1)) && add "  ^ poca memoria libera"

RSS="$(as_agent 'systemctl --user show hermes-gateway -p MemoryCurrent --value' 2>/dev/null)"
case "$RSS" in
  ''|*[!0-9]*) add "gateway: non leggibile" ;;
  *) add "gateway: $((RSS/1048576)) MB" ; [ "$RSS" -ge 734003200 ] && warn=$((warn+1)) && add "  ^ sopra i 700 MB" ;;
esac
UP="$(as_agent 'systemctl --user show hermes-gateway -p ActiveState --value' 2>/dev/null)"
[ "$UP" = "active" ] || { add "gateway: non attivo ($UP)"; fail=$((fail+1)); }

# --- google: a 7-day refresh token dies silently, so report its age
TOK="$AGENT_HOME/.hermes/google_token.json"
if [ -f "$TOK" ]; then
  AGE_D=$(( ( $(date +%s) - $(stat -c %Y "$TOK") ) / 86400 ))
  if as_agent 'hermes-python ~/.hermes/skills/productivity/google-workspace/scripts/setup.py --check' >/dev/null 2>&1; then
    add "google: token valido (aggiornato ${AGE_D}g fa)"
  else
    add "google: token non valido, riautorizza (setup.py --auth-url)"; fail=$((fail+1))
  fi
  [ "$AGE_D" -ge 5 ] && warn=$((warn+1)) && add "  ^ se l'app OAuth è in Testing scade al settimo giorno"
fi

# --- message
HEAD="Salute Hermes: $([ "$fail" -gt 0 ] && echo "$fail problemi" || echo "ok")"
[ "$warn" -gt 0 ] && HEAD="$HEAD, $warn avvisi"
printf '%s\n\n%s\n' "$HEAD" "$(printf '%s\n' "${lines[@]}")" | /usr/local/sbin/hermes-notify
