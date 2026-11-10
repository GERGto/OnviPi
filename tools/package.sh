#!/usr/bin/env bash
# Builds the installation package onvipi.tar.gz – also used for the OnviPiOS image.
#
#   tools/package.sh <version> <output-directory>
set -euo pipefail

VER="${1:?missing version}"
mkdir -p "${2:?missing output directory}"
OUT="$(cd "$2" && pwd)"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

cp -r "$ROOT/onvipi" "$STAGE/onvipi"
cp "$ROOT/README.md" "$ROOT/LICENSE" "$STAGE/onvipi/"
echo "$VER" >"$STAGE/onvipi/VERSION"
find "$STAGE/onvipi" -name __pycache__ -prune -exec rm -rf {} +
chmod 755 "$STAGE/onvipi/install.sh" "$STAGE"/onvipi/setup/*.sh "$STAGE"/onvipi/pi/*.py

tar -C "$STAGE" --owner=0 --group=0 --sort=name -czf "$OUT/onvipi.tar.gz" onvipi
echo "$OUT/onvipi.tar.gz ($VER)"
