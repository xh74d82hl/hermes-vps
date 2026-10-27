#!/usr/bin/env bash
# Send one Telegram alert when a systemd unit fails.
#
# Called by hermes-alert@<unit>.service, which is hooked to a unit via
#   OnFailure=hermes-alert@%n.service
# so %i here is the name of the unit that failed.
#
# Deliberately does not use `hermes send`: the gateway (or its Python env) may be
# exactly what is broken. This is curl + the Telegram Bot API, nothing else.
set -uo pipefail

UNIT="${1:-unknown-unit}"
ENVF="${ALERT_ENV_FILE:-/etc/hermes-alert.env}"
STATE_DIR="/run"

log() { printf '%s\n' "$*" >&2; }

if [ ! -r "$ENVF" ]; then
  log "no $ENVF (copy alert.example.env, fill the bot token and chat id)"
  exit 1
fi
# shellcheck disable=SC1090
set -a; . "$ENVF"; set +a
if [ -z "${ALERT_BOT_TOKEN:-}" ] || [ -z "${ALERT_CHAT_ID:-}" ]; then
  log "$ENVF must define ALERT_BOT_TOKEN and ALERT_CHAT_ID"
  exit 1
fi

# One alert per unit per 5 minutes: a failing unit that systemd restarts in a loop
# must not become a message loop.
STAMP="$STATE_DIR/hermes-alert.$(printf '%s' "$UNIT" | tr -c 'A-Za-z0-9_.-' '_')"
NOW="$(date +%s)"
if [ -f "$STAMP" ]; then
  LAST="$(cat "$STAMP" 2>/dev/null || echo 0)"
  case "$LAST" in ''|*[!0-9]*) LAST=0 ;; esac
  if [ $((NOW - LAST)) -lt 300 ]; then
    log "suppressed: an alert for $UNIT was sent less than 5 minutes ago"
    exit 0
  fi
fi
printf '%s\n' "$NOW" > "$STAMP" 2>/dev/null || true

# The last lines of the journal are what make an alert actionable.
TAIL="$(journalctl -u "$UNIT" -n 8 --no-pager -o cat 2>/dev/null | tail -8)"

MSG="$(printf '🔴 %s failed on %s\n\n%s' "$UNIT" "$(hostname)" "${TAIL:-no journal output}")"

if ! curl -fsS --max-time 20 \
  -X POST "https://api.telegram.org/bot${ALERT_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${ALERT_CHAT_ID}" \
  --data-urlencode "text=${MSG}" \
  --data-urlencode "disable_web_page_preview=true" >/dev/null; then
  log "telegram sendMessage failed"
  exit 1
fi
log "alert sent for $UNIT"
