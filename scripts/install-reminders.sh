#!/usr/bin/env bash
# Install the reminders layer: one CLI, one skill, one cron job per reminder.
#
#   sudo ./scripts/install-reminders.sh
#
# Installs:
#   /usr/local/bin/hermes-remind                     (shell wrapper: runs on the Google venv)
#   /home/<agent>/.hermes/scripts/hermes-remind.py   (the CLI + the fire path)
#   /home/<agent>/.hermes/skills/productivity/reminders/SKILL.md
#
# Needs: the google-workspace skill already authenticated (scripts/setup-google.sh era),
# ffmpeg + edge-tts only if you want --voice.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$HERE/vars.env"
AGENT_USER="${AGENT_USER:?}"
AGENT_HOME="/home/$AGENT_USER"
cd /

log() { printf '\n==> %s\n' "$*"; }

log "CLI"
install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 755 "$AGENT_HOME/.hermes/scripts"
install -o "$AGENT_USER" -g "$AGENT_USER" -m 755 \
  "$HERE/scripts/hermes-remind.py" "$AGENT_HOME/.hermes/scripts/hermes-remind.py"
echo "$AGENT_HOME/.hermes/scripts/hermes-remind.py"

log "Wrapper (must run under the interpreter that has the Google libraries)"
PY="$AGENT_HOME/.local/bin/hermes-python"
if [ -x "$PY" ]; then
  cat > /usr/local/bin/hermes-remind <<EOF
#!/bin/sh
exec "$PY" "$AGENT_HOME/.hermes/scripts/hermes-remind.py" "\$@"
EOF
  echo "using $PY"
else
  cat > /usr/local/bin/hermes-remind <<EOF
#!/bin/sh
# hermes-python not found: Calendar/Tasks mirrors will fail, Telegram reminders still work
exec /usr/bin/env python3 "$AGENT_HOME/.hermes/scripts/hermes-remind.py" "\$@"
EOF
  echo "WARN: $PY missing — installed with system python3 (no Google mirror)" >&2
fi
chmod 755 /usr/local/bin/hermes-remind

log "Skill"
install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 755 "$AGENT_HOME/.hermes/skills/productivity/reminders"
install -o "$AGENT_USER" -g "$AGENT_USER" -m 644 \
  "$HERE/skills/reminders/SKILL.md" "$AGENT_HOME/.hermes/skills/productivity/reminders/SKILL.md"
echo "$AGENT_HOME/.hermes/skills/productivity/reminders/SKILL.md"

log "Per-reminder wrapper dir"
install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 700 "$AGENT_HOME/.hermes/scripts/reminders"

log "Check"
sudo -u "$AGENT_USER" env HOME="$AGENT_HOME" hermes-remind list || true
sudo -u "$AGENT_USER" env HOME="$AGENT_HOME" hermes-remind --help >/dev/null && echo "CLI ok"

cat <<EOF

Next:
 1. Ask the agent: "ricordami di <cosa> domani alle 9"  (it should ask/confirm first)
 2. Check:  sudo -u $AGENT_USER env HOME=$AGENT_HOME hermes-remind list
 3. The job is visible with: hermes cron list   (name remind:r-xxxx)
 4. A reminder whose time passes while the gateway is down fires late, not never.
EOF
