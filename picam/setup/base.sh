#!/usr/bin/env bash
# PiCam – geräteunabhängiger Teil der Installation.
#
# Läuft an zwei Stellen und muss dort dasselbe ergeben:
#   * Image-Bau (Weg A): im chroot des Raspberry-Pi-OS-Images (image/build-image.sh)
#   * install.sh  (Weg B): direkt auf einem laufenden Pi
#
# Hier darf NICHTS Gerätespezifisches entstehen (Passwörter, Hostname, Webcam-Wahl,
# SSH-Schlüssel) – das erledigt setup/firstboot.sh auf dem jeweiligen Gerät.
set -euo pipefail

PKG="$(cd "$(dirname "$0")/.." && pwd)"          # Paketwurzel (picam/)
GO2RTC_VERSION="${GO2RTC_VERSION:-1.9.9}"

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mFEHLER: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "base.sh braucht root-Rechte."

case "$(dpkg --print-architecture)" in
  arm64) GOARCH=arm64 ;;
  armhf) GOARCH=arm ;;
  *) fail "Nicht unterstützte Architektur: $(dpkg --print-architecture)" ;;
esac

say "Pakete installieren"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
  ffmpeg v4l-utils iw rfkill dnsmasq-base python3 python3-paho-mqtt avahi-daemon curl \
  network-manager openssl fonts-dejavu-core ca-certificates

say "go2rtc v$GO2RTC_VERSION herunterladen ($GOARCH)"
curl -fSL --retry 3 -o /usr/local/bin/go2rtc.new \
  "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${GOARCH}"
chmod 755 /usr/local/bin/go2rtc.new
mv /usr/local/bin/go2rtc.new /usr/local/bin/go2rtc

say "PiCam-Dateien installieren"
id picam >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin picam
usermod -aG video picam
install -d -m 755 /opt/picam /opt/picam/setup
install -d -m 750 -g picam /etc/picam
install -m 755 "$PKG"/pi/*.py /opt/picam/
install -m 755 "$PKG"/setup/*.sh /opt/picam/setup/
[ -f "$PKG/VERSION" ] && install -m 644 "$PKG/VERSION" /opt/picam/VERSION
install -m 644 "$PKG"/systemd/*.service /etc/systemd/system/
install -d /etc/NetworkManager/dnsmasq-shared.d
install -m 644 "$PKG"/nm/picam-captive.conf /etc/NetworkManager/dnsmasq-shared.d/
ln -sf /opt/picam/picam_wifi.py /usr/local/bin/picam-wifi

say "Dienste aktivieren"
[ -d /run/systemd/system ] && systemctl daemon-reload   # im chroot läuft kein systemd
systemctl enable go2rtc.service picam-onvif.service picam-wsd.service picam-mqtt.service \
  picam-wifi.service
