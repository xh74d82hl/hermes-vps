#!/usr/bin/env bash
# Send one message to the agent's Telegram chat. Message text on stdin.
# Used by the ops jobs so they don't depend on the Hermes gateway being healthy.
set -uo pipefail

ENV_FILE="${HERMES_ENV_FILE:-/home/hermes/.hermes/.env}"
[ -r "$ENV_FILE" ] || { echo "cannot read $ENV_FILE" >&2; exit 1; }

TOKEN="$(grep -m1 '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
CHAT="$(grep -m1 '^TELEGRAM_HOME_CHANNEL=' "$ENV_FILE" | cut -d= -f2-)"
[ -n "$TOKEN" ] && [ -n "$CHAT" ] || { echo "TELEGRAM_BOT_TOKEN or HOME_CHANNEL missing" >&2; exit 1; }

BODY="$(cat)"
[ -n "${BODY//[[:space:]]/}" ] || exit 0

curl -fsS --max-time 25 -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${CHAT}" \
  --data-urlencode "text=${BODY}" \
  --data-urlencode "disable_web_page_preview=true" >/dev/null
