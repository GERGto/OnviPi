#!/usr/bin/env bash
# OnviPi – device-specific setup.
#
#   OnviPiOS image: runs once on first boot via onvipi-firstboot.service
#   Package:        called by install.sh, optionally with the camera name as argument
#
# Creates everything that must be unique per device: SSH host keys, hostname,
# camera password, setup hotspot name and the webcam detection.
# User, password, SSH, Wi-Fi country and time zone are set afterwards in the
# setup wizard (hotspot).
set -euo pipefail

NAME="$(printf '%s' "${1:-OnviPi}" | tr -d "'\"\\\\\`\$")"
CONF=/etc/onvipi/onvipi.conf

say()   { printf '==> %s\n' "$*"; }
rand()  { tr -dc "$1" </dev/urandom | head -c "$2" || true; }
ascii() { printf '%s\n' "$1" | iconv -f utf-8 -t ascii//TRANSLIT 2>/dev/null || printf '%s\n' "$1"; }

[ "$(id -u)" -eq 0 ] || { echo "firstboot.sh must run as root." >&2; exit 1; }

# Short ID from the MAC address – tells several OnviPis on the same network apart
MAC="$(cat /sys/class/net/wlan0/address 2>/dev/null || cat /sys/class/net/eth0/address 2>/dev/null || true)"
ID="$(printf '%s' "$MAC" | tr -dc '0-9a-f' | tail -c 4)"
[ -n "$ID" ] || ID="$(rand 'a-f0-9' 4)"

# SSH host keys (removed from the image so that devices do not share them)
if command -v ssh-keygen >/dev/null && ! ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1; then
  say "Generating SSH host keys"
  ssh-keygen -A
fi

# Wi-Fi country: explicit via WIFI_COUNTRY, otherwise only if none is set yet.
# Without a country the Wi-Fi stays blocked and the setup hotspot could not start.
CUR=""
if command -v raspi-config >/dev/null; then
  CUR="$(timeout 30 raspi-config nonint get_wifi_country 2>/dev/null || true)"
fi
[ "$CUR" = "00" ] && CUR=""
COUNTRY="${WIFI_COUNTRY:-${CUR:-DE}}"
if [ "$COUNTRY" != "$CUR" ]; then
  say "Setting Wi-Fi country to $COUNTRY"
  if command -v raspi-config >/dev/null; then
    timeout 60 raspi-config nonint do_wifi_country "$COUNTRY" || true
  else
    iw reg set "$COUNTRY" 2>/dev/null || true
  fi
fi
rfkill unblock wifi 2>/dev/null || true

# Change the hostname only while it is still the default
HOSTNAME_AUTO=0
CURHOST="$(cat /etc/hostname 2>/dev/null || hostname)"
if [ "$CURHOST" = "raspberrypi" ] || [ "$CURHOST" = "localhost" ]; then
  SLUG="$(ascii "$NAME" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9\n' '-' | sed 's/-\+/-/g;s/^-//;s/-$//')"
  if [ -z "$SLUG" ] || [ "$SLUG" = "onvipi" ]; then NEWHOST="onvipi-$ID"; else NEWHOST="onvipi-$SLUG"; fi
  NEWHOST="$(printf '%s' "$NEWHOST" | cut -c1-63 | sed 's/-$//')"
  say "Hostname -> $NEWHOST"
  echo "$NEWHOST" >/etc/hostname
  hostname "$NEWHOST" 2>/dev/null || true
  if grep -q '^127\.0\.1\.1' /etc/hosts; then
    sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$NEWHOST/" /etc/hosts
  else
    printf '127.0.1.1\t%s\n' "$NEWHOST" >>/etc/hosts
  fi
  HOSTNAME_AUTO=1
fi

NEW=0
if [ ! -f "$CONF" ]; then
  say "Creating configuration"
  if [ "$NAME" = "OnviPi" ]; then
    AP_SUFFIX="$(printf '%s' "$ID" | tr 'a-f' 'A-F')"
  else
    AP_SUFFIX="$(ascii "$NAME" | tr -c 'A-Za-z0-9\n' '-' | cut -c1-18)"
  fi
  install -d -m 750 -g onvipi /etc/onvipi
  cat >"$CONF.tmp" <<EOF
# OnviPi settings – after changes: sudo systemctl restart onvipi-go2rtc onvipi-wifi
CAM_NAME='$NAME'
HOSTNAME='$(cat /etc/hostname 2>/dev/null || hostname)'
HOSTNAME_AUTO='$HOSTNAME_AUTO'
WIFI_COUNTRY='$COUNTRY'
CAM_USER='admin'
CAM_PASSWORD='$(rand 'A-Za-z0-9' 16)'
AP_SSID='OnviPi-Setup-${AP_SUFFIX}'
AP_PASSWORD=''
WIFI_IFACE='wlan0'
ONVIF_PORT='8000'
VIDEO_DEVICE='/dev/video0'
VIDEO_FORMAT='mjpeg'
VIDEO_SIZE='1280x720'
FRAMERATE='15'
BITRATE='2500k'
HW_ENCODE='0'
ROTATE='0'
OVERLAY_TIME='1'
MOTION_ENABLE='1'
MOTION_SENSITIVITY='60'
MOTION_FPS='5'
MOTION_COOLDOWN='8'
EOF
  mv "$CONF.tmp" "$CONF"
  NEW=1
fi
chown root:onvipi "$CONF"
chmod 640 "$CONF"

# Webcam: full detection for a new configuration, otherwise only check that it still fits.
# (At boot the webcam may not be ready yet – go2rtc checks again on every start.)
say "Detecting webcam"
if [ "$NEW" = 1 ]; then
  python3 /opt/onvipi/onvipi_camera.py --detect --render
else
  python3 /opt/onvipi/onvipi_camera.py --ensure --render
fi
