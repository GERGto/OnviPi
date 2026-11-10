#!/usr/bin/env bash
# Generates the release notes: download links, quick start and the matching
# section from CHANGELOG.md.
#
#   tools/release-notes.sh <version> <tag> <owner/repo> [base-image-url] [go2rtc-version]
set -euo pipefail

VER="${1:?missing version}"
TAG="${2:?missing tag}"
REPO="${3:?missing repository}"
BASE="${4:-}"
GO2RTC="${5:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DL="https://github.com/$REPO/releases/download/$TAG"

# Section "## [<version>] …" up to the next version heading
CHANGES="$(awk -v v="$VER" '
  index($0, "## [" v "]") == 1 { on = 1; next }
  on && /^## \[/ { exit }
  on { print }
' "$ROOT/CHANGELOG.md" | sed '/./,$!d')"
[ -n "$CHANGES" ] || CHANGES="See [CHANGELOG.md](https://github.com/$REPO/blob/$TAG/CHANGELOG.md)."

cat <<EOF
## ⬇️ Downloads

| File | Use |
|---|---|
| **[onvipios-arm64.img.xz]($DL/onvipios-arm64.img.xz)** | **OnviPiOS** – ready-to-flash image for a new device, set up from your phone |
| **[onvipi.tar.gz]($DL/onvipi.tar.gz)** | **Package** for an existing Raspberry Pi OS |
| [SHA256SUMS]($DL/SHA256SUMS) | Checksums |

## 🚀 Quick start

**OnviPiOS image**
1. Flash [\`onvipios-arm64.img.xz\`]($DL/onvipios-arm64.img.xz) with [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
   (*Use custom*, OS customisation: **No**).
2. Plug in the webcam, power on the Pi and after 2–3 minutes join the Wi-Fi **\`OnviPi-Setup-XXXX\`**.
3. The wizard guides you through camera, preview, access (user, password, SSH, country, time zone) and Wi-Fi.

**Package on an existing Raspberry Pi OS (Lite 64-bit, Bookworm/Trixie)**
\`\`\`bash
curl -fsSL $DL/onvipi.tar.gz | tar xz
sudo bash onvipi/install.sh "Garden"
\`\`\`
An existing PiCam installation is migrated automatically – settings and the Home Assistant device are kept.

Full documentation: [README](https://github.com/$REPO/blob/$TAG/README.md)

## 📝 Changes

$CHANGES
EOF

if [ -n "$BASE$GO2RTC" ]; then
  printf '\n---\n<sub>'
  [ -n "$BASE" ] && printf 'Based on %s' "$(basename "$BASE")"
  [ -n "$BASE" ] && [ -n "$GO2RTC" ] && printf ' · '
  [ -n "$GO2RTC" ] && printf 'go2rtc %s' "$GO2RTC"
  printf '</sub>\n'
fi
