#!/usr/bin/env bash
# Install the finance layer: one CLI that turns statement PDFs into a reconciled
# ledger, plus the skill that fires it when a PDF lands in the Telegram chat.
#
#   sudo ./scripts/install-finance.sh
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
  "$HERE/scripts/finance-ledger.py" "$AGENT_HOME/.hermes/scripts/finance-ledger.py"
echo "$AGENT_HOME/.hermes/scripts/finance-ledger.py"

log "Wrapper"
cat > /usr/local/bin/finance-ledger <<EOF
#!/bin/sh
exec /usr/bin/env python3 "$AGENT_HOME/.hermes/scripts/finance-ledger.py" "\$@"
EOF
chmod 755 /usr/local/bin/finance-ledger

log "Skill"
install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 755 "$AGENT_HOME/.hermes/skills/productivity/finance"
install -o "$AGENT_USER" -g "$AGENT_USER" -m 644 \
  "$HERE/skills/finance/SKILL.md" "$AGENT_HOME/.hermes/skills/productivity/finance/SKILL.md"

log "Data dir"
install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 700 "$AGENT_HOME/.hermes/finance"

log "Dependencies"
command -v pdftotext >/dev/null 2>&1 && echo "pdftotext ok" || echo "WARN: install poppler-utils (pdftotext)" >&2

log "Check"
sudo -u "$AGENT_USER" env HOME="$AGENT_HOME" finance-ledger list 2>&1 | head -5 || true

cat <<EOF

Next:
 1. Export a statement PDF from your bank and send it to the Telegram bot.
 2. The agent imports it, reports period/rows/reconciled and attaches the dashboard.
 3. Data: $AGENT_HOME/.hermes/finance/finance.db · dashboard.html
EOF
