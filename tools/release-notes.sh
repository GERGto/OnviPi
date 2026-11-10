#!/usr/bin/env bash
# Erzeugt die Release-Notes: Download-Links, Kurzanleitung und den passenden
# Abschnitt aus CHANGELOG.md.
#
#   tools/release-notes.sh <version> <tag> <owner/repo> [basis-image-url] [go2rtc-version]
set -euo pipefail

VER="${1:?Version fehlt}"
TAG="${2:?Tag fehlt}"
REPO="${3:?Repository fehlt}"
BASE="${4:-}"
GO2RTC="${5:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DL="https://github.com/$REPO/releases/download/$TAG"

# Abschnitt "## [<version>] …" bis zur nächsten Versionsüberschrift
CHANGES="$(awk -v v="$VER" '
  index($0, "## [" v "]") == 1 { on = 1; next }
  on && /^## \[/ { exit }
  on { print }
' "$ROOT/CHANGELOG.md" | sed '/./,$!d')"
[ -n "$CHANGES" ] || CHANGES="Siehe [CHANGELOG.md](https://github.com/$REPO/blob/$TAG/CHANGELOG.md)."

cat <<EOF
## ⬇️ Downloads

| Datei | Wofür |
|---|---|
| **[picam-arm64.img.xz]($DL/picam-arm64.img.xz)** | **Weg A:** fertiges Image zum Flashen – neues Gerät, Einrichtung per Handy |
| **[picam.tar.gz]($DL/picam.tar.gz)** | **Weg B:** Paket für ein vorhandenes Raspberry Pi OS |
| [SHA256SUMS]($DL/SHA256SUMS) | Prüfsummen |

## 🚀 Installation

**Weg A – Image**
1. [\`picam-arm64.img.xz\`]($DL/picam-arm64.img.xz) mit dem [Raspberry Pi Imager](https://www.raspberrypi.com/software/) flashen
   (*Eigenes Image verwenden*, eigene Einstellungen: **Nein**).
2. Webcam anstecken, Pi starten und nach 2–3 Minuten mit dem WLAN **\`PiCam-Setup-XXXX\`** verbinden.
3. Der Assistent führt durch Kamera, Vorschau, Zugang (Benutzer, Passwort, SSH, Land, Zeitzone) und WLAN.

**Weg B – vorhandenes Raspberry Pi OS (Lite 64-bit, Bookworm/Trixie)**
\`\`\`bash
curl -fsSL $DL/picam.tar.gz | tar xz
sudo bash picam/install.sh "Keller"
\`\`\`

Ausführliche Anleitung: [README](https://github.com/$REPO/blob/$TAG/README.md)

## 📝 Änderungen

$CHANGES
EOF

if [ -n "$BASE$GO2RTC" ]; then
  printf '\n---\n<sub>'
  [ -n "$BASE" ] && printf 'Basis: %s' "$(basename "$BASE")"
  [ -n "$BASE" ] && [ -n "$GO2RTC" ] && printf ' · '
  [ -n "$GO2RTC" ] && printf 'go2rtc %s' "$GO2RTC"
  printf '</sub>\n'
fi
