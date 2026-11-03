#!/usr/bin/env bash
# Print the text of the most recent morning brief, ready to be rewritten for speech.
# Deterministic: the voice job should never have to work out which file to read.
set -uo pipefail

H="${HERMES_HOME:-$HOME/.hermes}"
[ -f "$H/cron/jobs.json" ] || exit 0

ID="$(python3 - "$H/cron/jobs.json" <<'PY'
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
[ -n "$ID" ] || exit 0

F="$(ls -t "$H/cron/output/$ID"/*.md 2>/dev/null | head -1)"
[ -n "$F" ] && [ -s "$F" ] || exit 0

awk '/^## Response/{flag=1; next} flag' "$F" \
  | grep -v '^MEDIA:' \
  | sed '/^[[:space:]]*$/d' \
  | head -60
