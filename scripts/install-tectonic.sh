#!/usr/bin/env bash
# Install Tectonic (single-binary LaTeX engine) for the agent user, from the GitHub release asset.
# Much lighter than TeX Live: packages are fetched on demand and cached in ~/.cache/Tectonic.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$HERE/vars.env"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT; chmod 755 "$TMP"

URL="$(curl -fsSL https://api.github.com/repos/tectonic-typesetting/tectonic/releases/latest \
  | jq -r '.assets[].browser_download_url | select(test("x86_64-unknown-linux-gnu\\.tar\\.gz$"))' | head -1)"
[ -n "$URL" ] || { echo "could not find a release asset; install manually" >&2; exit 1; }
echo "Downloading $URL"
curl -fsSL "$URL" -o "$TMP/tectonic.tgz"
echo "SHA256: $(sha256sum "$TMP/tectonic.tgz" | cut -d' ' -f1)"
tar -xzf "$TMP/tectonic.tgz" -C "$TMP"
install -d -o "$AGENT_USER" -g "$AGENT_USER" "/home/$AGENT_USER/.local/bin"
install -m 755 -o "$AGENT_USER" -g "$AGENT_USER" "$TMP/tectonic" "/home/$AGENT_USER/.local/bin/tectonic"
echo "Installed. Test as $AGENT_USER on YOUR real CV: tectonic main.tex  (watch 'free -h' meanwhile)"
echo "If a custom class/package fails, compile in GitHub Actions instead."
