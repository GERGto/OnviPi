#!/usr/bin/env bash
# Baut das Installationspaket picam.tar.gz (Weg B) – wird auch für das Image verwendet.
#
#   tools/package.sh <version> <ausgabeverzeichnis>
set -euo pipefail

VER="${1:?Version fehlt}"
mkdir -p "${2:?Ausgabeverzeichnis fehlt}"
OUT="$(cd "$2" && pwd)"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

cp -r "$ROOT/picam" "$STAGE/picam"
cp "$ROOT/README.md" "$ROOT/LICENSE" "$STAGE/picam/"
echo "$VER" >"$STAGE/picam/VERSION"
find "$STAGE/picam" -name __pycache__ -prune -exec rm -rf {} +
chmod 755 "$STAGE/picam/install.sh" "$STAGE"/picam/setup/*.sh "$STAGE"/picam/pi/*.py

tar -C "$STAGE" --owner=0 --group=0 --sort=name -czf "$OUT/picam.tar.gz" picam
echo "$OUT/picam.tar.gz ($VER)"
