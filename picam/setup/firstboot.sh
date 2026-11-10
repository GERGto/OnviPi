#!/usr/bin/env bash
# PiCam – gerätespezifische Einrichtung.
#
#   Image (Weg A): einmal beim ersten Start über picam-firstboot.service
#   Paket (Weg B): von install.sh aufgerufen, optional mit Kameraname als Argument
#
# Erzeugt alles, was pro Gerät eindeutig sein muss: SSH-Hostschlüssel, Hostname,
# Kamera-Passwort, Name des Einrichtungs-WLANs und die Webcam-Erkennung.
# Benutzer, Passwort, SSH, WLAN-Land und Zeitzone stellt man danach im
# Einrichtungs-Assistenten (Hotspot) ein.
set -euo pipefail

NAME="$(printf '%s' "${1:-PiCam}" | tr -d "'\"\\\\\`\$")"
CONF=/etc/picam/picam.conf

say()   { printf '==> %s\n' "$*"; }
rand()  { tr -dc "$1" </dev/urandom | head -c "$2" || true; }
ascii() { printf '%s\n' "$1" | iconv -f utf-8 -t ascii//TRANSLIT 2>/dev/null || printf '%s\n' "$1"; }

[ "$(id -u)" -eq 0 ] || { echo "firstboot.sh braucht root-Rechte." >&2; exit 1; }

# Kurzkennung aus der MAC-Adresse – unterscheidet mehrere PiCams im selben Netz
MAC="$(cat /sys/class/net/wlan0/address 2>/dev/null || cat /sys/class/net/eth0/address 2>/dev/null || true)"
ID="$(printf '%s' "$MAC" | tr -dc '0-9a-f' | tail -c 4)"
[ -n "$ID" ] || ID="$(rand 'a-f0-9' 4)"

# SSH-Hostschlüssel (im Image entfernt, damit nicht alle Geräte dieselben haben)
if command -v ssh-keygen >/dev/null && ! ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1; then
  say "SSH-Hostschlüssel erzeugen"
  ssh-keygen -A
fi

# WLAN-Land: explizit über WIFI_COUNTRY, sonst nur setzen, wenn noch keins gesetzt ist.
# Ohne Land bleibt das WLAN gesperrt und der Einrichtungs-Hotspot könnte nicht starten.
CUR=""
if command -v raspi-config >/dev/null; then
  CUR="$(timeout 30 raspi-config nonint get_wifi_country 2>/dev/null || true)"
fi
[ "$CUR" = "00" ] && CUR=""
COUNTRY="${WIFI_COUNTRY:-${CUR:-DE}}"
if [ "$COUNTRY" != "$CUR" ]; then
  say "WLAN-Land auf $COUNTRY setzen"
  if command -v raspi-config >/dev/null; then
    timeout 60 raspi-config nonint do_wifi_country "$COUNTRY" || true
  else
    iw reg set "$COUNTRY" 2>/dev/null || true
  fi
fi
rfkill unblock wifi 2>/dev/null || true

# Hostname nur ändern, wenn noch der Standardname gesetzt ist
HOSTNAME_AUTO=0
CURHOST="$(cat /etc/hostname 2>/dev/null || hostname)"
if [ "$CURHOST" = "raspberrypi" ] || [ "$CURHOST" = "localhost" ]; then
  SLUG="$(ascii "$NAME" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9\n' '-' | sed 's/-\+/-/g;s/^-//;s/-$//')"
  if [ -z "$SLUG" ] || [ "$SLUG" = "picam" ]; then NEWHOST="picam-$ID"; else NEWHOST="picam-$SLUG"; fi
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
  say "Konfiguration anlegen"
  if [ "$NAME" = "PiCam" ]; then
    AP_SUFFIX="$(printf '%s' "$ID" | tr 'a-f' 'A-F')"
  else
    AP_SUFFIX="$(ascii "$NAME" | tr -c 'A-Za-z0-9\n' '-' | cut -c1-18)"
  fi
  install -d -m 750 -g picam /etc/picam
  cat >"$CONF.tmp" <<EOF
# PiCam-Einstellungen – nach Änderungen: sudo systemctl restart go2rtc picam-wifi
CAM_NAME='$NAME'
HOSTNAME='$(cat /etc/hostname 2>/dev/null || hostname)'
HOSTNAME_AUTO='$HOSTNAME_AUTO'
WIFI_COUNTRY='$COUNTRY'
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
  mv "$CONF.tmp" "$CONF"
  NEW=1
fi
chown root:picam "$CONF"
chmod 640 "$CONF"

# Webcam: bei neuer Konfiguration vollständig erkennen, sonst nur prüfen, ob sie noch passt.
# (Beim Booten kann die Webcam noch fehlen – go2rtc prüft bei jedem Start erneut.)
say "Webcam erkennen"
if [ "$NEW" = 1 ]; then
  python3 /opt/picam/picam_camera.py --detect --render
else
  python3 /opt/picam/picam_camera.py --ensure --render
fi
