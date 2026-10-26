#!/usr/bin/env bash
# Read-only health check for the box. Run as root:  sudo ./scripts/doctor.sh
# Changes nothing; prints OK / WARN / FAIL per check and exits non-zero if anything FAILs.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
. "$HERE/vars.env"
AGENT_USER="${AGENT_USER:-hermes}"
AGENT_HOME="/home/$AGENT_USER"
AGENT_UID="$(id -u "$AGENT_USER" 2>/dev/null || echo 0)"
FAIL=0

ok()   { printf '  OK    %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; FAIL=$((FAIL+1)); }
head_() { printf '\n%s\n' "$*"; }

as_agent() { sudo -iu "$AGENT_USER" env HOME="$AGENT_HOME" XDG_RUNTIME_DIR="/run/user/$AGENT_UID" PATH="$AGENT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" bash -lc "$*"; }

head_ "System"
printf '  %s | %s | load %s\n' "$(hostname)" "$(. /etc/os-release && echo "$PRETTY_NAME")" "$(cut -d' ' -f1-3 /proc/loadavg)"
free -h | sed -n '1,2p' | sed 's/^/  /'
df -h / | tail -1 | sed 's/^/  /'
swapon --show | grep -q . && ok "swap active" || bad "no swap"

head_ "SSH hardening"
if [ -f /etc/ssh/sshd_config.d/00-hardening.conf ]; then
  ok "hardening drop-in present"
  grep -qi '^PasswordAuthentication no' /etc/ssh/sshd_config.d/00-hardening.conf \
    && ok "password auth disabled" || bad "password auth still enabled"
  grep -qi '^PermitRootLogin no' /etc/ssh/sshd_config.d/00-hardening.conf \
    && ok "root login disabled" || warn "root login not disabled"
else
  warn "no /etc/ssh/sshd_config.d/00-hardening.conf"
fi

head_ "Firewall"
if command -v ufw >/dev/null 2>&1; then
  if ufw status | grep -q 'Status: active'; then
    ok "ufw active"; ufw status numbered | grep -E 'ALLOW' | sed 's/^/        /'
  else
    bad "ufw inactive"
  fi
else
  warn "ufw not installed"
fi

head_ "Users"
id "$AGENT_USER" >/dev/null 2>&1 && ok "agent user $AGENT_USER exists" || bad "agent user $AGENT_USER missing"
if id -nG "$AGENT_USER" 2>/dev/null | grep -qwE 'sudo|docker'; then
  bad "$AGENT_USER is in sudo/docker group (should not be)"
else
  ok "$AGENT_USER has no sudo/docker"
fi
loginctl show-user "$AGENT_USER" -p Linger --value 2>/dev/null | grep -q yes \
  && ok "linger enabled (user services survive reboot)" || warn "linger disabled"

head_ "Hermes gateway"
if as_agent 'systemctl --user is-active hermes-gateway' 2>/dev/null | grep -q '^active'; then
  ok "hermes-gateway active"
  as_agent 'systemctl --user show hermes-gateway -p ActiveEnterTimestamp --value'
else
  bad "hermes-gateway not active"
fi
as_agent 'hermes config check 2>&1 | grep -E "Config version" ' | sed 's/^/  /'

head_ "Secrets permissions"
for f in "$AGENT_HOME/.hermes/.env" "$AGENT_HOME/.hermes/google_token.json" \
         "$AGENT_HOME/.hermes/google_client_secret.json" /etc/hermes-backup.env; do
  [ -e "$f" ] || continue
  mode="$(stat -c '%a' "$f")"
  case "$mode" in
    600|400) ok "$(basename "$f") $mode" ;;
    *) bad "$(basename "$f") is $mode (should be 600)" ;;
  esac
done

head_ "Voice"
command -v ffmpeg >/dev/null 2>&1 && ok "ffmpeg $(ffmpeg -version | head -1 | awk '{print $3}')" || warn "ffmpeg missing (voice bubbles need it)"
PY=""; for p in "$AGENT_HOME"/.hermes/tools/python-*/bin/python3; do [ -x "$p" ] && PY="$p"; done
if [ -n "$PY" ] && sudo -u "$AGENT_USER" "$PY" -c 'import edge_tts' >/dev/null 2>&1; then
  ok "edge-tts importable in the gateway interpreter"
else
  warn "edge-tts not importable (run scripts/install-voice.sh)"
fi
as_agent 'grep -q "^GROQ_API_KEY=." ~/.hermes/.env 2>/dev/null' && ok "GROQ_API_KEY set" || warn "GROQ_API_KEY not set (voice input off)"
as_agent 'grep -qE "^voice:|auto_tts:" ~/.hermes/config.yaml 2>/dev/null' \
  && ok "voice block present" || warn "no voice block in config.yaml"

head_ "Google"
if [ -e "$AGENT_HOME/.hermes/google_token.json" ]; then
  S="$AGENT_HOME/.hermes/skills/productivity/google-workspace/scripts/setup.py"
  if [ -f "$S" ] && as_agent "$AGENT_HOME/.local/bin/hermes-python $S --check" >/dev/null 2>&1; then
    ok "google auth valid (--check)"
  else
    warn "google token present but --check failed (re-authorize: setup.py --auth-url)"
  fi
else
  warn "google not configured (optional)"
fi

head_ "Telegram / cron"
as_agent 'hermes pairing list 2>/dev/null | sed -n "/Approved/,/^$/p"' | sed 's/^/  /'
JOBS="$(as_agent 'hermes cron list 2>/dev/null | grep -c "\[active\]"')"
[ "${JOBS:-0}" -ge 1 ] && ok "$JOBS active cron job(s)" || warn "no active cron jobs"

head_ "Backups"
if systemctl list-timers --all 2>/dev/null | grep -q hermes-backup; then
  ok "hermes-backup.timer installed"
  systemctl is-enabled hermes-backup.timer >/dev/null 2>&1 && ok "timer enabled" || warn "timer not enabled"
  [ -f /etc/hermes-backup.env ] && [ "$(stat -c '%a' /etc/hermes-backup.env)" = 600 ] \
    && ok "/etc/hermes-backup.env 600" || warn "/etc/hermes-backup.env missing or not 600"
  systemctl is-failed --quiet hermes-backup.service 2>/dev/null && bad "last backup run failed"
else
  warn "no backup timer (run scripts/setup-backup.sh)"
fi

printf '\n'
[ "$FAIL" -eq 0 ] && echo "All clear ($FAIL failures)." || echo "$FAIL check(s) FAILED."
exit $(( FAIL > 0 ))
