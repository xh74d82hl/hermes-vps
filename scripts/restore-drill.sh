#!/usr/bin/env bash
# Quarterly restore drill: a backup nobody restores is a hope, not a backup.
# Restores the newest snapshot into a temp dir, verifies the config and the SQLite
# dump, reports one line, then deletes everything.
set -uo pipefail

ENVF="/etc/hermes-backup.env"
[ -r "$ENVF" ] || { echo "no $ENVF" >&2; exit 1; }
# shellcheck disable=SC1090
set -a; . "$ENVF"; set +a
export HOME=/root

TMP="$(mktemp -d /tmp/hermes-drill.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

note() { printf '%s\n' "$*" | /usr/local/sbin/hermes-notify; }

if ! restic snapshots --latest 1 >/dev/null 2>&1; then
  note "Drill di ripristino FALLITO: il repository non risponde."
  exit 1
fi

if ! restic restore latest --target "$TMP" --quiet; then
  note "Drill di ripristino FALLITO: restore non riuscito."
  exit 1
fi

FILES="$(find "$TMP" -type f | wc -l)"
CONFIG="$(find "$TMP" -name config.yaml -path '*/.hermes/*' | head -1)"
DUMP="$(find "$TMP" -name 'state.db*.dump' | head -1)"
problems=()

[ -n "$CONFIG" ] && [ -s "$CONFIG" ] || problems+=("config.yaml mancante")
if [ -n "$DUMP" ] && [ -s "$DUMP" ]; then
  INTEG="$(sqlite3 "$DUMP" 'PRAGMA integrity_check;' 2>&1 | head -1)"
  [ "$INTEG" = "ok" ] || problems+=("integrity_check: $INTEG")
  TABLES="$(sqlite3 "$DUMP" "SELECT count(*) FROM sqlite_master WHERE type='table';" 2>/dev/null)"
  case "${TABLES:-}" in
    ''|*[!0-9]*) problems+=("sqlite3 non ha risposto sul dump") ;;
    *) [ "$TABLES" -ge 10 ] || problems+=("solo ${TABLES} tabelle nel dump") ;;
  esac
else
  problems+=("nessun dump del database")
fi

if [ "${#problems[@]}" -eq 0 ]; then
  note "Drill di ripristino ok: ${FILES} file ripristinati, config.yaml presente, database integro (${TABLES} tabelle)."
else
  note "Drill di ripristino FALLITO: $(printf '%s; ' "${problems[@]}")"
  exit 1
fi
