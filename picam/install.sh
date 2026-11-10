#!/usr/bin/env bash
# PiCam – Raspberry Pi + USB-Webcam als Überwachungskamera für Home Assistant
#
#   sudo ./install.sh "Garten"            # Name der Kamera (Standard: PiCam)
#   WIFI_COUNTRY=AT sudo ./install.sh     # WLAN-Land (Standard: DE)
#
# Getestet für Raspberry Pi OS (Bookworm/Trixie, 64-bit, Lite) auf dem Pi 4.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || exec sudo -E "$0" "$@"

NAME="$(printf '%s' "${1:-PiCam}" | tr -d "'\"\\\\\`\$")"
WIFI_COUNTRY="${WIFI_COUNTRY:-DE}"
HERE="$(cd "$(dirname "$0")" && pwd)"
CONF=/etc/picam/picam.conf

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mFEHLER: %s\033[0m\n' "$*" >&2; exit 1; }
rand() { tr -dc "$1" </dev/urandom | head -c "$2" || true; }
ascii() { printf '%s\n' "$1" | iconv -f utf-8 -t ascii//TRANSLIT 2>/dev/null || printf '%s\n' "$1"; }

case "$(uname -m)" in
  aarch64) GOARCH=arm64 ;;
  armv7l|armv6l) GOARCH=arm ;;
  *) fail "Nicht unterstützte Architektur: $(uname -m)" ;;
esac

say "Pakete installieren"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  ffmpeg v4l-utils iw dnsmasq-base python3 python3-paho-mqtt avahi-daemon curl network-manager

systemctl is-active --quiet NetworkManager \
  || fail "NetworkManager läuft nicht. Bitte Raspberry Pi OS Bookworm oder neuer verwenden."

say "WLAN-Land auf $WIFI_COUNTRY setzen"
if command -v raspi-config >/dev/null; then raspi-config nonint do_wifi_country "$WIFI_COUNTRY" || true; fi
rfkill unblock wifi 2>/dev/null || true

say "go2rtc herunterladen ($GOARCH)"
curl -fSL --retry 3 -o /usr/local/bin/go2rtc.new \
  "https://github.com/AlexxIT/go2rtc/releases/latest/download/go2rtc_linux_${GOARCH}"
chmod 755 /usr/local/bin/go2rtc.new
mv /usr/local/bin/go2rtc.new /usr/local/bin/go2rtc
/usr/local/bin/go2rtc -version || true

say "Dateien installieren"
id picam >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin picam
usermod -aG video picam
install -d -m 755 /opt/picam
install -d -m 750 -g picam /etc/picam
install -m 755 "$HERE"/pi/*.py /opt/picam/
install -m 644 "$HERE"/systemd/*.service /etc/systemd/system/
install -d /etc/NetworkManager/dnsmasq-shared.d
install -m 644 "$HERE"/nm/picam-captive.conf /etc/NetworkManager/dnsmasq-shared.d/
ln -sf /opt/picam/picam_wifi.py /usr/local/bin/picam-wifi

# Hostname nur ändern, wenn noch der Standardname gesetzt ist
if [ "$(hostname)" = "raspberrypi" ]; then
  NEWHOST="picam-$(ascii "$NAME" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9\n' '-' | sed 's/-\+/-/g;s/^-//;s/-$//')"
  NEWHOST="${NEWHOST%-}"
  say "Hostname -> $NEWHOST"
  hostnamectl set-hostname "$NEWHOST"
  sed -i "s/\braspberrypi\b/$NEWHOST/g" /etc/hosts
fi

if [ ! -f "$CONF" ]; then
  say "Konfiguration anlegen"
  AP_SUFFIX="$(ascii "$NAME" | tr -c 'A-Za-z0-9\n' '-' | cut -c1-18)"
  cat >"$CONF" <<EOF
# PiCam-Einstellungen – nach Änderungen: sudo systemctl restart go2rtc picam-wifi
CAM_NAME='$NAME'
HOSTNAME='$(hostname)'
CAM_USER='admin'
CAM_PASSWORD='$(rand 'A-Za-z0-9' 16)'
AP_SSID='PiCam-Setup-${AP_SUFFIX}'
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
fi
chown root:picam "$CONF"; chmod 640 "$CONF"

# Optional: MQTT-Zugang direkt bei der Installation setzen
#   MQTT_USER=picam MQTT_PASSWORD=geheim sudo -E ./install.sh "Garten"
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

say "Webcam erkennen"
python3 /opt/picam/picam_camera.py --detect --render

say "Dienste starten"
systemctl daemon-reload
systemctl enable --now go2rtc.service picam-onvif.service picam-wsd.service picam-mqtt.service
systemctl enable picam-wifi.service
systemctl restart picam-wifi.service

# shellcheck disable=SC1090
. "$CONF"
IP="$(hostname -I | awk '{print $1}')"
cat <<EOF

────────────────────────────────────────────────────────────
 PiCam „$CAM_NAME“ ist installiert
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
