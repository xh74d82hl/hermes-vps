#!/usr/bin/env bash
# Install Hermes Agent as the unprivileged agent user. Installer runs non-interactive; you then
# run `hermes setup` (needs a TTY) yourself so tokens/keys never touch this repo.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$HERE/vars.env"
AGENT_UID="$(id -u "$AGENT_USER")"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o "$TMP/install.sh"
echo "Installer SHA256: $(sha256sum "$TMP/install.sh" | cut -d' ' -f1)"
echo "Skim it first if you want: less $TMP/install.sh (Ctrl-C to abort, Enter to continue)"
read -r _

chmod 755 "$TMP"; chmod 644 "$TMP/install.sh"
sudo -u "$AGENT_USER" -H bash "$TMP/install.sh" --non-interactive

cat <<EOF

Hermes installed for $AGENT_USER. Now configure it interactively:

  sudo -iu $AGENT_USER env XDG_RUNTIME_DIR=/run/user/$AGENT_UID bash -l
  hermes setup                 # provider: opencode-go (OPENCODE_GO_API_KEY), Telegram bot token
  hermes gateway install       # creates its own systemd user service
  systemctl --user status hermes-gateway   # service name may differ; check 'hermes gateway --help'

Secrets live in ~$AGENT_USER/.hermes/.env (chmod 600). Never paste them into chat or git.
Recommended default model for cheap routine work: DeepSeek V4.1 Flash.
EOF
