#!/usr/bin/env bash
# OnviPi – turns a Raspberry Pi with a USB webcam into an ONVIF camera for Home Assistant.
#
# Installation on an existing Raspberry Pi OS:
#   sudo bash install.sh "Garden"                   # camera name (default: OnviPi)
#   sudo WIFI_COUNTRY=GB bash install.sh "Garden"   # Wi-Fi country (default: DE)
#
# The OnviPiOS image uses the same steps: setup/base.sh during the build,
# setup/firstboot.sh on first boot. Running it again updates OnviPi (settings are kept).
# An existing PiCam installation (former name) is migrated automatically.
# Tested on Raspberry Pi OS (Bookworm/Trixie, 64-bit, Lite) on a Pi 4.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || exec sudo -E bash "$0" "$@"

HERE="$(cd "$(dirname "$0")" && pwd)"
CONF=/etc/onvipi/onvipi.conf

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

bash "$HERE/setup/migrate.sh"
bash "$HERE/setup/base.sh"

systemctl is-active --quiet NetworkManager \
  || fail "NetworkManager is not running. Please use Raspberry Pi OS Bookworm or newer."

WIFI_COUNTRY="${WIFI_COUNTRY:-DE}" bash /opt/onvipi/setup/firstboot.sh "${1:-OnviPi}"

# Optional: set MQTT credentials during installation
#   sudo MQTT_USER=onvipi MQTT_PASSWORD=secret bash install.sh "Garden"
if [ -n "${MQTT_USER:-}" ]; then
  say "Saving MQTT credentials"
  MQTT_HOST="${MQTT_HOST:-homeassistant.local}" MQTT_PORT="${MQTT_PORT:-1883}" python3 - <<'EOF'
import os, sys
sys.path.insert(0, "/opt/onvipi")
from onvipilib import MQTT_CONF, save_conf
save_conf(MQTT_CONF, {k: os.environ.get(k, "") for k in
          ("MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_PASSWORD")} | {"SNAPSHOT_INTERVAL": "30"},
          mode=0o600)
EOF
fi

say "Starting services"
systemctl daemon-reload
systemctl restart onvipi-go2rtc.service onvipi-onvif.service onvipi-wsd.service \
  onvipi-mqtt.service onvipi-wifi.service

# shellcheck disable=SC1090
. "$CONF"
IP="$(hostname -I | awk '{print $1}')"
cat <<EOF

────────────────────────────────────────────────────────────
 OnviPi “$CAM_NAME” is installed ($(cat /opt/onvipi/VERSION 2>/dev/null || echo dev))
────────────────────────────────────────────────────────────
 Settings:             http://$(hostname).local   (or http://$IP, login below)
 Live view (browser):  http://$(hostname).local:1984   (or http://$IP:1984)
 RTSP:                 rtsp://$CAM_USER:$CAM_PASSWORD@$IP:8554/onvipi

 Home Assistant → Settings → Devices & services → Add integration → ONVIF
   Select the device “$(echo "$CAM_NAME" | tr -c 'A-Za-z0-9_.\n-' '_')”
   Username: $CAM_USER
   Password: $CAM_PASSWORD

 Setup hotspot (appears when no network is available):
   Name:     $AP_SSID
   Password: ${AP_PASSWORD:-(open – no password)}
   Portal:   http://10.42.0.1

 These details are also in $CONF (sudo cat $CONF).
 Set up Wi-Fi again:  sudo onvipi-wifi reset
────────────────────────────────────────────────────────────
EOF
