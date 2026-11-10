#!/usr/bin/env bash
# PiCam – Raspberry Pi + USB-Webcam als Überwachungskamera für Home Assistant
#
# Installation auf einem vorhandenen Raspberry Pi OS (Weg B):
#   sudo bash install.sh "Garten"            # Name der Kamera (Standard: PiCam)
#   WIFI_COUNTRY=AT sudo bash install.sh     # WLAN-Land (Standard: DE)
#
# Das fertige Image (Weg A) nutzt dieselben Schritte: setup/base.sh beim Bau,
# setup/firstboot.sh beim ersten Start. Erneut ausführen = Update (Einstellungen bleiben).
# Getestet für Raspberry Pi OS (Bookworm/Trixie, 64-bit, Lite) auf dem Pi 4.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || exec sudo -E bash "$0" "$@"

HERE="$(cd "$(dirname "$0")" && pwd)"
CONF=/etc/picam/picam.conf

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mFEHLER: %s\033[0m\n' "$*" >&2; exit 1; }

bash "$HERE/setup/base.sh"

systemctl is-active --quiet NetworkManager \
  || fail "NetworkManager läuft nicht. Bitte Raspberry Pi OS Bookworm oder neuer verwenden."

WIFI_COUNTRY="${WIFI_COUNTRY:-DE}" bash /opt/picam/setup/firstboot.sh "${1:-PiCam}"

# Optional: MQTT-Zugang direkt bei der Installation setzen
#   MQTT_USER=picam MQTT_PASSWORD=geheim sudo -E bash install.sh "Garten"
if [ -n "${MQTT_USER:-}" ]; then
  say "MQTT-Zugang speichern"
  MQTT_HOST="${MQTT_HOST:-homeassistant.local}" MQTT_PORT="${MQTT_PORT:-1883}" python3 - <<'EOF'
import os, sys
sys.path.insert(0, "/opt/picam")
from picamlib import MQTT_CONF, save_conf
save_conf(MQTT_CONF, {k: os.environ.get(k, "") for k in
          ("MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_PASSWORD")} | {"SNAPSHOT_INTERVAL": "30"},
          mode=0o600)
EOF
fi

say "Dienste starten"
systemctl daemon-reload
systemctl restart go2rtc.service picam-onvif.service picam-wsd.service picam-mqtt.service \
  picam-wifi.service

# shellcheck disable=SC1090
. "$CONF"
IP="$(hostname -I | awk '{print $1}')"
cat <<EOF

────────────────────────────────────────────────────────────
 PiCam „$CAM_NAME“ ist installiert ($(cat /opt/picam/VERSION 2>/dev/null || echo dev))
────────────────────────────────────────────────────────────
 Einstellungen:        http://$(hostname).local   (oder http://$IP, Login wie unten)
 Livebild (Browser):   http://$(hostname).local:1984   (oder http://$IP:1984)
 RTSP:                 rtsp://$CAM_USER:$CAM_PASSWORD@$IP:8554/picam

 Home Assistant → Einstellungen → Geräte & Dienste → Integration hinzufügen → ONVIF
   Gerät „$(echo "$CAM_NAME" | tr -c 'A-Za-z0-9_.\n-' '_')“ auswählen
   Benutzer: $CAM_USER
   Passwort: $CAM_PASSWORD

 Einrichtungs-WLAN (erscheint, wenn kein Netz verfügbar ist):
   Name:     $AP_SSID
   Passwort: ${AP_PASSWORD:-(offen – kein Passwort)}
   Portal:   http://10.42.0.1

 Diese Daten stehen auch in $CONF (sudo cat $CONF).
 WLAN neu einrichten:  sudo picam-wifi reset
────────────────────────────────────────────────────────────
EOF
