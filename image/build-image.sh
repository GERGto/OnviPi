#!/usr/bin/env bash
# Baut das PiCam-Image (Weg A) aus dem offiziellen Raspberry Pi OS Lite (64-bit).
#
#   sudo image/build-image.sh <entpacktes-paket/picam> <ausgabe.img.xz>
#
# Läuft auf einem arm64-Linux-Host (GitHub-Runner ubuntu-24.04-arm) – nativ, ohne Emulation.
# Benötigt: curl xz-utils fdisk(sfdisk) e2fsprogs jq, optional zerofree.
#
# Umgebungsvariablen:
#   BASE_IMAGE_URL   Basis-Image (Standard: aktuelles Raspberry Pi OS Lite arm64)
#   EXTRA_MB         Zusätzlicher Platz für die Pakete beim Bau (Standard 1536)
#   WORK             Arbeitsverzeichnis (Standard ./build)
#   GO2RTC_VERSION   wird an setup/base.sh durchgereicht
set -euo pipefail

PKG="$(realpath "${1:?Paketverzeichnis fehlt}")"
OUT="$(realpath -m "${2:?Ausgabedatei fehlt}")"
BASE_IMAGE_URL="${BASE_IMAGE_URL:-https://downloads.raspberrypi.com/raspios_lite_arm64_latest}"
EXTRA_MB="${EXTRA_MB:-1536}"
WORK="$(realpath -m "${WORK:-build}")"
HEADROOM_MB=128          # freier Platz im verkleinerten Image (wird beim 1. Start ohnehin vergrößert)

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mFEHLER: %s\033[0m\n' "$*" >&2; exit 1; }
fsck_ok() { e2fsck -fy "$1" || [ $? -le 1 ]; }   # 1 = Fehler behoben, ist ok

[ "$(id -u)" -eq 0 ] || fail "Bitte mit sudo starten."
[ "$(uname -m)" = "aarch64" ] || fail "Nur auf arm64-Hosts (z. B. ubuntu-24.04-arm)."
[ -f "$PKG/setup/base.sh" ] || fail "$PKG ist kein PiCam-Paket."

mkdir -p "$WORK" "$(dirname "$OUT")"
IMG="$WORK/picam.img"
ROOT="$WORK/root"
LOOP=""

cleanup() {
  set +e
  for m in "$ROOT/dev/pts" "$ROOT/dev" "$ROOT/proc" "$ROOT/sys" "$ROOT/boot/firmware" "$ROOT"; do
    mountpoint -q "$m" && umount -l "$m"
  done
  [ -n "$LOOP" ] && losetup -d "$LOOP" 2>/dev/null
}
trap cleanup EXIT

# ------------------------------------------------------------------ Basis-Image laden
say "Basis-Image ermitteln"
URL="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$BASE_IMAGE_URL")"
echo "$URL"
echo "$URL" >"$WORK/base-image-url.txt"
curl -fSL --retry 3 -o "$WORK/base.img.xz" "$URL"
if curl -fsSL -o "$WORK/base.img.xz.sha256" "$URL.sha256"; then
  WANT="$(awk '{print $1}' "$WORK/base.img.xz.sha256")"
  GOT="$(sha256sum "$WORK/base.img.xz" | awk '{print $1}')"
  [ "$WANT" = "$GOT" ] || fail "Prüfsumme des Basis-Images stimmt nicht."
  echo "Prüfsumme ok: $GOT"
else
  echo "Hinweis: keine .sha256 zum Basis-Image gefunden – Prüfung übersprungen."
fi

say "Entpacken und vergrößern (+${EXTRA_MB} MB)"
xz -dc -T0 "$WORK/base.img.xz" >"$IMG"
rm -f "$WORK/base.img.xz"
truncate -s "+${EXTRA_MB}M" "$IMG"
echo ",+" | sfdisk -N 2 --no-reread -q "$IMG"
LOOP="$(losetup -fP --show "$IMG")"
fsck_ok "${LOOP}p2"
resize2fs "${LOOP}p2"

# ------------------------------------------------------------------ chroot vorbereiten
say "Image einhängen"
mkdir -p "$ROOT"
mount "${LOOP}p2" "$ROOT"
BOOT="$ROOT/boot/firmware"
[ -d "$BOOT" ] || BOOT="$ROOT/boot"
mount "${LOOP}p1" "$BOOT"
mount --bind /dev "$ROOT/dev"
mount --bind /dev/pts "$ROOT/dev/pts"
mount -t proc proc "$ROOT/proc"
mount -t sysfs sys "$ROOT/sys"

# DNS im chroot: echte Upstream-Server des Hosts (nicht den lokalen Stub)
mv "$ROOT/etc/resolv.conf" "$ROOT/etc/resolv.conf.picam" 2>/dev/null || true
cp -L "$( [ -f /run/systemd/resolve/resolv.conf ] && echo /run/systemd/resolve/resolv.conf || echo /etc/resolv.conf)" \
  "$ROOT/etc/resolv.conf"
# Keine Dienste im chroot starten
printf '#!/bin/sh\nexit 101\n' >"$ROOT/usr/sbin/policy-rc.d"
chmod 755 "$ROOT/usr/sbin/policy-rc.d"

in_chroot() {
  chroot "$ROOT" /usr/bin/env -i HOME=/root TERM=dumb LANG=C.UTF-8 \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    DEBIAN_FRONTEND=noninteractive GO2RTC_VERSION="${GO2RTC_VERSION:-}" "$@"
}

# ------------------------------------------------------------------ PiCam installieren
say "PiCam installieren"
rm -rf "$ROOT/tmp/picam-src"
cp -r "$PKG" "$ROOT/tmp/picam-src"
in_chroot bash /tmp/picam-src/setup/base.sh      # leere GO2RTC_VERSION -> Standard aus base.sh

say "Erststart vorbereiten"
in_chroot systemctl enable picam-firstboot.service
# Kein Abfrage-Dialog für Benutzername/Passwort auf dem Bildschirm –
# Benutzer, Passwort und SSH kommen aus dem Hotspot-Assistenten.
in_chroot systemctl disable userconfig.service 2>/dev/null || true
# Standard-Zeitzone für die Uhrzeit im Bild (im Assistenten änderbar)
ln -sf /usr/share/zoneinfo/Europe/Berlin "$ROOT/etc/localtime"
echo "Europe/Berlin" >"$ROOT/etc/timezone"
cp "$PKG/VERSION" "$ROOT/etc/picam-release" 2>/dev/null || true

# ------------------------------------------------------------------ Aufräumen
say "Aufräumen"
in_chroot apt-get clean
rm -rf "$ROOT"/var/lib/apt/lists/* "$ROOT/tmp/picam-src" "$ROOT/usr/sbin/policy-rc.d"
rm -f "$ROOT/etc/resolv.conf"
mv "$ROOT/etc/resolv.conf.picam" "$ROOT/etc/resolv.conf" 2>/dev/null || true
# Pro Gerät neu erzeugen: machine-id (ONVIF-Geräte-ID!) und SSH-Hostschlüssel
: >"$ROOT/etc/machine-id"
rm -f "$ROOT/var/lib/dbus/machine-id" && ln -s /etc/machine-id "$ROOT/var/lib/dbus/machine-id"
rm -f "$ROOT"/etc/ssh/ssh_host_* "$ROOT/root/.bash_history"
find "$ROOT/var/log" -type f -exec truncate -s 0 {} +

for m in "$ROOT/dev/pts" "$ROOT/dev" "$ROOT/proc" "$ROOT/sys" "$BOOT" "$ROOT"; do
  umount "$m"
done

# ------------------------------------------------------------------ verkleinern
say "Dateisystem verkleinern"
fsck_ok "${LOOP}p2"
resize2fs -M "${LOOP}p2"
BSIZE="$(dumpe2fs -h "${LOOP}p2" 2>/dev/null | awk -F: '/^Block size/{gsub(/ /,"",$2);print $2}')"
BLOCKS="$(dumpe2fs -h "${LOOP}p2" 2>/dev/null | awk -F: '/^Block count/{gsub(/ /,"",$2);print $2}')"
FS_BYTES=$(( BLOCKS * BSIZE + HEADROOM_MB * 1024 * 1024 ))
fsck_ok "${LOOP}p2"
resize2fs "${LOOP}p2" "$(( FS_BYTES / 1024 ))K"
command -v zerofree >/dev/null && zerofree "${LOOP}p2"   # bessere Kompression
losetup -d "$LOOP"
LOOP=""

START="$(sfdisk -J "$IMG" | jq '.partitiontable.partitions[1].start')"
SECTORS=$(( (FS_BYTES + 511) / 512 ))
echo "${START},${SECTORS}" | sfdisk -N 2 --no-reread -q "$IMG"
truncate -s $(( (START + SECTORS) * 512 )) "$IMG"
echo "Image: $(( $(stat -c %s "$IMG") / 1024 / 1024 )) MB"

say "Komprimieren"
xz -T0 -6 -c "$IMG" >"$OUT"
rm -f "$IMG"
ls -lh "$OUT"
