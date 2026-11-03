#!/usr/bin/env bash
# Speak a text file and print MEDIA:<path> for the cron delivery.
#
# The caller (voice-brief job) hands over a text that was already rewritten for
# listening. This script is the second safety net: it strips what never works spoken
# (URLs, markdown, bullets, emoji), turns amounts and times into words, and truncates
# at a sentence boundary so a bad day cannot produce a four-minute monologue.
set -uo pipefail

IN="${1:-}"
[ -n "$IN" ] && [ -s "$IN" ] || exit 0

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
VOICE="${BRIEF_VOICE:-it-IT-ElsaNeural}"
MAX_CHARS="${BRIEF_MAX_CHARS:-1600}"
STAMP="$(date +%F-%H%M)"
OUT="/tmp/hermes-brief-$STAMP.ogg"
MP3="/tmp/hermes-brief-$STAMP.mp3"

TEXT="$(python3 - "$IN" "$MAX_CHARS" <<'PY'
import re, sys

raw = open(sys.argv[1], encoding="utf-8", errors="replace").read()
limit = int(sys.argv[2])

t = raw
t = re.sub(r"https?://\S+|www\.\S+", " ", t)          # links are useless out loud
t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)        # markdown links -> label
t = re.sub(r"^[#>\-\*\u2022]+\s*", "", t, flags=re.M)  # headings and bullets
t = re.sub(r"[*_`~]+", "", t)
t = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", t)  # emoji
t = re.sub(r"MEDIA:\S+", " ", t)
t = re.sub(r"\s*\n\s*", ". ", t)                      # newlines become pauses
t = re.sub(r"\.(\s*\.)+", ".", t)
t = re.sub(r"\s{2,}", " ", t).strip()

# amounts and clock times read badly as symbols
t = re.sub(r"€\s*(\d+)[.,](\d{2})", r"\1 euro e \2 centesimi", t)
t = re.sub(r"€\s*(\d+)", r"\1 euro", t)
t = re.sub(r"\b(\d{2}):(\d{2})\b", lambda m: f"{int(m.group(1))} e {m.group(2)}", t)

if len(t) > limit:
    cut = t.rfind(".", 0, limit)
    t = t[: cut + 1] if cut > limit // 2 else t[:limit].rsplit(" ", 1)[0] + "."
    t += " Il resto nella versione scritta."

print(t)
PY
)"

[ -n "${TEXT//[[:space:]]/}" ] || exit 0

PY_BIN=""
for candidate in "$HERMES_HOME"/tools/python-*/bin/python3; do
  [ -x "$candidate" ] || continue
  PY_BIN="$candidate"
done
[ -n "$PY_BIN" ] || { echo "no hermes python with edge-tts" >&2; exit 1; }

"$PY_BIN" -m edge_tts --voice "$VOICE" --text "$TEXT" --write-media "$MP3" >/dev/null 2>&1 || exit 1
ffmpeg -y -loglevel error -i "$MP3" -c:a libopus -b:a 48k "$OUT" >/dev/null 2>&1 || exit 1
rm -f "$MP3"
[ -s "$OUT" ] && printf 'MEDIA:%s\n' "$OUT"
