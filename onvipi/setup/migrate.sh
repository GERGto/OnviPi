#!/usr/bin/env bash
# OnviPi – takes over an existing PiCam installation (the project's former name).
#
# Called by install.sh before base.sh. Does nothing if there is no PiCam install or if
# OnviPi is already configured. Keeps all settings, the camera password and saved
# Wi-Fi networks. The ONVIF device ID stays the same, so Home Assistant keeps the device.
set -euo pipefail

OLD_CONF=/etc/picam/picam.conf
NEW_CONF=/etc/onvipi/onvipi.conf

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

[ -f "$OLD_CONF" ] || exit 0
[ -f "$NEW_CONF" ] && exit 0

say "Migrating existing PiCam installation to OnviPi"

# Stop the old services (they hold the same ports)
OLD_UNITS="go2rtc.service picam-onvif.service picam-wsd.service picam-mqtt.service picam-wifi.service picam-firstboot.service"
for u in $OLD_UNITS; do
  systemctl disable --now "$u" 2>/dev/null || true   # one by one: missing units are fine
done

# Take over the settings
install -d -m 750 /etc/onvipi
cp -a "$OLD_CONF" "$NEW_CONF"
sed -i "s/^AP_SSID='PiCam-Setup-/AP_SSID='OnviPi-Setup-/" "$NEW_CONF"
sed -i "1s/.*/# OnviPi settings – after changes: sudo systemctl restart onvipi-go2rtc onvipi-wifi/" "$NEW_CONF"
[ -f /etc/picam/mqtt.conf ] && cp -a /etc/picam/mqtt.conf /etc/onvipi/mqtt.conf
echo "Settings copied to $NEW_CONF (hostname is kept)."

# Remove the old installation; keep the old config as a backup
for u in $OLD_UNITS; do rm -f "/etc/systemd/system/$u"; done
rm -rf /opt/picam
rm -f /usr/local/bin/picam-wifi /etc/NetworkManager/dnsmasq-shared.d/picam-captive.conf
nmcli con delete picam-hotspot >/dev/null 2>&1 || true
mv /etc/picam "/etc/picam.migrated-$(date +%Y%m%d)"
id picam >/dev/null 2>&1 && userdel picam 2>/dev/null || true
systemctl daemon-reload
echo "Old files removed, backup: /etc/picam.migrated-*"
