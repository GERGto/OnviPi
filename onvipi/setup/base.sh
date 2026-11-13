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
TTYD_VERSION=1.7.7                                # checksums below belong to this version

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
  network-manager openssl fonts-dejavu-core ca-certificates sudo
# NTFS-formatted USB storage for recordings (older kernels without the ntfs3 driver). Optional.
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends ntfs-3g \
  || echo "Note: package 'ntfs-3g' is not available."

say "Downloading go2rtc v$GO2RTC_VERSION ($GOARCH)"
curl -fSL --retry 3 -o /usr/local/bin/go2rtc.new \
  "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${GOARCH}"
chmod 755 /usr/local/bin/go2rtc.new
mv /usr/local/bin/go2rtc.new /usr/local/bin/go2rtc

# Terminal in the web interface (expert mode). Raspberry Pi OS has no ttyd package, so the
# official static binary is used – pinned by version and checksum. Optional: without it the
# terminal page says so.
if [ "$GOARCH" = arm64 ]; then
  TTYD_FILE=ttyd.aarch64; TTYD_SHA=b38acadd89d1d396a0f5649aa52c539edbad07f4bc7348b27b4f4b7219dd4165
else
  TTYD_FILE=ttyd.armhf;   TTYD_SHA=8240c8438b68d3b10b0e1a4e7c914d70fca6a7606b516f40bf40adfa1044d801
fi
if ! echo "$TTYD_SHA  /usr/local/bin/ttyd" | sha256sum -c - >/dev/null 2>&1; then
  say "Downloading ttyd $TTYD_VERSION"
  if curl -fSL --retry 3 -o /usr/local/bin/ttyd.new \
       "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/${TTYD_FILE}" \
     && echo "$TTYD_SHA  /usr/local/bin/ttyd.new" | sha256sum -c - >/dev/null; then
    chmod 755 /usr/local/bin/ttyd.new
    mv /usr/local/bin/ttyd.new /usr/local/bin/ttyd
  else
    rm -f /usr/local/bin/ttyd.new
    echo "Note: ttyd could not be installed – the browser terminal stays disabled."
  fi
fi

say "Installing OnviPi files"
id onvipi >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin onvipi
usermod -aG video,audio onvipi
install -d -m 755 /opt/onvipi /opt/onvipi/setup /opt/onvipi/web
install -d -m 750 -g onvipi /etc/onvipi
install -m 755 "$PKG"/pi/*.py /opt/onvipi/
rm -f /opt/onvipi/web/*
install -m 644 "$PKG"/pi/web/* /opt/onvipi/web/
install -m 755 "$PKG"/setup/*.sh /opt/onvipi/setup/
[ -f "$PKG/VERSION" ] && install -m 644 "$PKG/VERSION" /opt/onvipi/VERSION
install -m 644 "$PKG"/systemd/*.service "$PKG"/systemd/*.timer /etc/systemd/system/
install -d -m 755 /var/lib/onvipi
install -d /etc/NetworkManager/dnsmasq-shared.d
install -m 644 "$PKG"/nm/onvipi-captive.conf /etc/NetworkManager/dnsmasq-shared.d/
ln -sf /opt/onvipi/onvipi_wifi.py /usr/local/bin/onvipi-wifi

# The ONVIF service runs as the unprivileged user 'onvipi'. Home Assistant's buttons
# "Reboot" and "Set system date and time" need root – only via this small helper.
SUDOERS=/etc/sudoers.d/onvipi
cat >"$SUDOERS.tmp" <<'EOF'
onvipi ALL=(root) NOPASSWD: /usr/bin/python3 /opt/onvipi/onvipi_priv.py reboot
onvipi ALL=(root) NOPASSWD: /usr/bin/python3 /opt/onvipi/onvipi_priv.py set-time [0-9]*
EOF
chmod 440 "$SUDOERS.tmp"
if visudo -cf "$SUDOERS.tmp" >/dev/null; then
  mv "$SUDOERS.tmp" "$SUDOERS"
else
  rm -f "$SUDOERS.tmp"
  echo "Note: could not install $SUDOERS – reboot from Home Assistant will not work."
fi

say "Enabling services"
[ -d /run/systemd/system ] && systemctl daemon-reload   # no systemd inside the chroot
systemctl enable onvipi-go2rtc.service onvipi-onvif.service onvipi-wsd.service \
  onvipi-mqtt.service onvipi-record.service onvipi-wifi.service onvipi-autoupdate.timer
