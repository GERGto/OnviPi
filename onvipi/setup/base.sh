#!/usr/bin/env bash
# OnviPi – device-independent part of the installation.
#
# Runs in two places and must produce the same result in both:
#   * image build (OnviPiOS): inside the chroot of the Raspberry Pi OS image
#   * install.sh (package):   directly on a running Pi
#
# Nothing device-specific may be created here (passwords, hostname, webcam choice,
# SSH keys) – setup/firstboot.sh does that on each device.
set -euo pipefail

PKG="$(cd "$(dirname "$0")/.." && pwd)"          # package root (onvipi/)
GO2RTC_VERSION="${GO2RTC_VERSION:-1.9.9}"

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "base.sh must run as root."

case "$(dpkg --print-architecture)" in
  arm64) GOARCH=arm64 ;;
  armhf) GOARCH=arm ;;
  *) fail "Unsupported architecture: $(dpkg --print-architecture)" ;;
esac

say "Installing packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
  ffmpeg v4l-utils iw rfkill dnsmasq-base python3 python3-paho-mqtt avahi-daemon curl \
  network-manager openssl fonts-dejavu-core ca-certificates

say "Downloading go2rtc v$GO2RTC_VERSION ($GOARCH)"
curl -fSL --retry 3 -o /usr/local/bin/go2rtc.new \
  "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${GOARCH}"
chmod 755 /usr/local/bin/go2rtc.new
mv /usr/local/bin/go2rtc.new /usr/local/bin/go2rtc

say "Installing OnviPi files"
id onvipi >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin onvipi
usermod -aG video onvipi
install -d -m 755 /opt/onvipi /opt/onvipi/setup
install -d -m 750 -g onvipi /etc/onvipi
install -m 755 "$PKG"/pi/*.py /opt/onvipi/
install -m 755 "$PKG"/setup/*.sh /opt/onvipi/setup/
[ -f "$PKG/VERSION" ] && install -m 644 "$PKG/VERSION" /opt/onvipi/VERSION
install -m 644 "$PKG"/systemd/*.service /etc/systemd/system/
install -d /etc/NetworkManager/dnsmasq-shared.d
install -m 644 "$PKG"/nm/onvipi-captive.conf /etc/NetworkManager/dnsmasq-shared.d/
ln -sf /opt/onvipi/onvipi_wifi.py /usr/local/bin/onvipi-wifi

say "Enabling services"
[ -d /run/systemd/system ] && systemctl daemon-reload   # no systemd inside the chroot
systemctl enable onvipi-go2rtc.service onvipi-onvif.service onvipi-wsd.service \
  onvipi-mqtt.service onvipi-wifi.service
