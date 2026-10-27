#!/usr/bin/env bash
# Voice output for Hermes: ffmpeg (mp3 -> ogg/opus for Telegram voice bubbles) + edge-tts
# installed into the interpreter THE GATEWAY actually runs.
#
# Run as root:  sudo ./scripts/install-voice.sh
# Speech input (Groq / local whisper) needs no system package: see SETUP.md §6.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
. "$HERE/vars.env"
AGENT_USER="${AGENT_USER:?}"
AGENT_HOME="/home/$AGENT_USER"
# Neutral cwd: uv discovers pyproject.toml / uv.toml by walking up from the current directory, and
# as the agent user it cannot read the admin's home (this cost us a "Permission denied: uv.toml").
cd /

log() { printf '\n==> %s\n' "$*"; }

log "ffmpeg"
if command -v ffmpeg >/dev/null 2>&1; then
  echo "already installed: $(ffmpeg -version | head -1)"
else
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y --no-install-recommends ffmpeg
  ffmpeg -version | head -1
fi

log "Locating the gateway interpreter"
PY=""
for p in "$AGENT_HOME"/.hermes/tools/python-*/bin/python3; do
  [ -x "$p" ] || continue
  PY="$p"   # glob expands in version order; last = newest
done
if [ -z "$PY" ]; then
  echo "no gateway interpreter found under $AGENT_HOME/.hermes/tools/python-*/bin/python3" >&2
  echo "install Hermes first (scripts/install-hermes.sh)" >&2
  exit 1
fi
echo "using: $PY"
"$PY" --version

log "Installing edge-tts into that interpreter"
UV=""
for u in "$AGENT_HOME"/.hermes/tools/uv-*/uv; do
  [ -x "$u" ] || continue
  UV="$u"
done
if [ -n "$UV" ]; then
  sudo -u "$AGENT_USER" env HOME="$AGENT_HOME" "$UV" pip install --python "$PY" edge-tts
else
  sudo -u "$AGENT_USER" env HOME="$AGENT_HOME" "$PY" -m pip install --user edge-tts
fi

log "Verify"
sudo -u "$AGENT_USER" env HOME="$AGENT_HOME" "$PY" -c "import edge_tts; print('edge_tts OK', edge_tts.__file__)"

log "Smoke test (Italian, no network model needed)"
TMP="$(mktemp -d)"
chown "$AGENT_USER" "$TMP"   # the smoke test runs as the agent user; this dir is created by root
trap 'rm -rf "$TMP"' EXIT
sudo -u "$AGENT_USER" env HOME="$AGENT_HOME" "$PY" -m edge_tts --voice it-IT-ElsaNeural \
  --text "Ciao, il test vocale funziona." --write-media "$TMP/a.mp3" >/dev/null
ffmpeg -y -loglevel error -i "$TMP/a.mp3" -c:a libopus -b:a 48k "$TMP/a.ogg"
echo "OK: $(ls -l "$TMP/a.ogg" | awk '{print $5}') bytes of Ogg/Opus"

cat <<EOF

Next:
 1. Add the stt / tts / voice blocks to ~$AGENT_USER/.hermes/config.yaml
    (see examples/config.snippets.yaml), then: hermes config check
 2. hermes tools enable stt tts --platform telegram
 3. hermes gateway restart
 4. Send the bot a voice note. Default behaviour: transcribed, answered in text.
 5. Voice on demand / for scheduled jobs:
      $PY -m edge_tts --voice it-IT-ElsaNeural --text "..." --write-media /tmp/a.mp3
      ffmpeg -y -loglevel error -i /tmp/a.mp3 -c:a libopus -b:a 48k /tmp/a.ogg
      hermes send -t telegram:<CHAT_ID> "MEDIA:/tmp/a.ogg"
EOF
