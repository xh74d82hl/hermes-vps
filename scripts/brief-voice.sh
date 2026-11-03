#!/usr/bin/env bash
# Turn the morning brief into a voice note.
#
# Runs as a no-agent cron job a few minutes after the brief: it reads the newest
# output file of the "morning-brief" job, synthesizes it with edge-tts and prints
# MEDIA:<path>, which the scheduler delivers as a Telegram voice message.
# No model involved, so it costs nothing and cannot fail on quota.
set -uo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
VOICE="${BRIEF_VOICE:-it-IT-ElsaNeural}"
OUT="/tmp/hermes-brief-$(date +%F).ogg"
MP3="/tmp/hermes-brief-$(date +%F).mp3"

# job id of the morning brief, from the cron store
JOB_ID="$(python3 - "$HERMES_HOME/cron/jobs.json" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(0)
jobs = data.get("jobs", []) if isinstance(data, dict) else data
for job in jobs:
    if job.get("name") == "morning-brief":
        print(job.get("id", ""))
        break
PY
)"
[ -n "$JOB_ID" ] || exit 0

LATEST="$(ls -t "$HERMES_HOME/cron/output/$JOB_ID"/*.md 2>/dev/null | head -1)"
[ -n "$LATEST" ] || exit 0

# the delivered text is the section after "## Response"
TEXT="$(awk '/^## Response/{flag=1; next} flag' "$LATEST" | grep -v '^MEDIA:' | sed '/^[[:space:]]*$/N;/^\n$/D')"
[ -n "${TEXT//[[:space:]]/}" ] || exit 0
printf '%s' "$TEXT" > /tmp/hermes-brief.txt

PY=""
for candidate in "$HERMES_HOME"/tools/python-*/bin/python3; do
  [ -x "$candidate" ] || continue
  PY="$candidate"
done
[ -n "$PY" ] || exit 0

"$PY" -m edge_tts --voice "$VOICE" --text "$TEXT" --write-media "$MP3" >/dev/null 2>&1 || exit 0
ffmpeg -y -loglevel error -i "$MP3" -c:a libopus -b:a 48k "$OUT" >/dev/null 2>&1 || exit 0
rm -f "$MP3" /tmp/hermes-brief.txt
[ -s "$OUT" ] && printf 'MEDIA:%s\n' "$OUT"
