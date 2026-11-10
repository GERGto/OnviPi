#!/usr/bin/env bash
# Builds OnviPiOS – the ready-to-flash OnviPi image – from the official
# Raspberry Pi OS Lite (64-bit).
#
#   sudo image/build-image.sh <unpacked-package/onvipi> <output.img.xz>
#
# Runs natively on an arm64 Linux host (GitHub runner ubuntu-24.04-arm), no emulation.
# Requires: curl xz-utils fdisk (sfdisk) e2fsprogs jq, optionally zerofree.
#
# Environment:
#   BASE_IMAGE_URL   base image (default: latest Raspberry Pi OS Lite arm64)
#   EXTRA_MB         extra space for packages during the build (default 1536)
#   WORK             working directory (default ./build)
#   GO2RTC_VERSION   passed through to setup/base.sh
set -euo pipefail

PKG="$(realpath "${1:?missing package directory}")"
OUT="$(realpath -m "${2:?missing output file}")"
BASE_IMAGE_URL="${BASE_IMAGE_URL:-https://downloads.raspberrypi.com/raspios_lite_arm64_latest}"
EXTRA_MB="${EXTRA_MB:-1536}"
WORK="$(realpath -m "${WORK:-build}")"
HEADROOM_MB=128          # free space left in the shrunk image (expanded on first boot anyway)

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
fsck_ok() { e2fsck -fy "$1" || [ $? -le 1 ]; }   # 1 = errors corrected, that is fine

[ "$(id -u)" -eq 0 ] || fail "Please run with sudo."
[ "$(uname -m)" = "aarch64" ] || fail "arm64 hosts only (e.g. ubuntu-24.04-arm)."
[ -f "$PKG/setup/base.sh" ] || fail "$PKG is not an OnviPi package."
VERSION="$(cat "$PKG/VERSION" 2>/dev/null || echo dev)"

mkdir -p "$WORK" "$(dirname "$OUT")"
IMG="$WORK/onvipios.img"
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

# ------------------------------------------------------------------ base image
say "Resolving base image"
URL="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$BASE_IMAGE_URL")"
echo "$URL"
echo "$URL" >"$WORK/base-image-url.txt"
curl -fSL --retry 3 -o "$WORK/base.img.xz" "$URL"
if curl -fsSL -o "$WORK/base.img.xz.sha256" "$URL.sha256"; then
  WANT="$(awk '{print $1}' "$WORK/base.img.xz.sha256")"
  GOT="$(sha256sum "$WORK/base.img.xz" | awk '{print $1}')"
  [ "$WANT" = "$GOT" ] || fail "Checksum of the base image does not match."
  echo "Checksum ok: $GOT"
else
  echo "Note: no .sha256 found for the base image – check skipped."
fi

say "Unpacking and growing (+${EXTRA_MB} MB)"
xz -dc -T0 "$WORK/base.img.xz" >"$IMG"
rm -f "$WORK/base.img.xz"
truncate -s "+${EXTRA_MB}M" "$IMG"
echo ",+" | sfdisk -N 2 --no-reread -q "$IMG"
LOOP="$(losetup -fP --show "$IMG")"
fsck_ok "${LOOP}p2"
resize2fs "${LOOP}p2"

# ------------------------------------------------------------------ chroot
say "Mounting image"
mkdir -p "$ROOT"
mount "${LOOP}p2" "$ROOT"
BOOT="$ROOT/boot/firmware"
[ -d "$BOOT" ] || BOOT="$ROOT/boot"
mount "${LOOP}p1" "$BOOT"
mount --bind /dev "$ROOT/dev"
mount --bind /dev/pts "$ROOT/dev/pts"
mount -t proc proc "$ROOT/proc"
mount -t sysfs sys "$ROOT/sys"

# DNS inside the chroot: the host's real upstream servers (not the local stub)
mv "$ROOT/etc/resolv.conf" "$ROOT/etc/resolv.conf.onvipi" 2>/dev/null || true
cp -L "$( [ -f /run/systemd/resolve/resolv.conf ] && echo /run/systemd/resolve/resolv.conf || echo /etc/resolv.conf)" \
  "$ROOT/etc/resolv.conf"
# Do not start services inside the chroot
printf '#!/bin/sh\nexit 101\n' >"$ROOT/usr/sbin/policy-rc.d"
chmod 755 "$ROOT/usr/sbin/policy-rc.d"

in_chroot() {
  chroot "$ROOT" /usr/bin/env -i HOME=/root TERM=dumb LANG=C.UTF-8 \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    DEBIAN_FRONTEND=noninteractive GO2RTC_VERSION="${GO2RTC_VERSION:-}" "$@"
}

# ------------------------------------------------------------------ install OnviPi
say "Installing OnviPi"
rm -rf "$ROOT/tmp/onvipi-src"
cp -r "$PKG" "$ROOT/tmp/onvipi-src"
in_chroot bash /tmp/onvipi-src/setup/base.sh      # empty GO2RTC_VERSION -> default from base.sh

say "Preparing first boot"
in_chroot systemctl enable onvipi-firstboot.service
# No username/password prompt on the screen – user, password and SSH come from the
# setup wizard on the hotspot.
in_chroot systemctl disable userconfig.service 2>/dev/null || true
# Default time zone for the timestamp in the picture (changeable in the wizard)
ln -sf /usr/share/zoneinfo/Europe/Berlin "$ROOT/etc/localtime"
echo "Europe/Berlin" >"$ROOT/etc/timezone"

# OnviPiOS identification (os-release stays Raspberry Pi OS so apt and tools keep working)
cat >"$ROOT/etc/onvipios-release" <<EOF
NAME="OnviPiOS"
VERSION="$VERSION"
BASE_IMAGE="$(basename "$URL")"
HOME_URL="https://github.com/GERGto/OnviPi"
EOF
cat >"$ROOT/etc/motd" <<EOF

  OnviPiOS $VERSION – ONVIF camera for Home Assistant
  Settings:  http://<hostname>.local  ·  Status: sudo onvipi-wifi status
  Docs:      https://github.com/GERGto/OnviPi

EOF

# ------------------------------------------------------------------ clean up
say "Cleaning up"
in_chroot apt-get clean
rm -rf "$ROOT"/var/lib/apt/lists/* "$ROOT/tmp/onvipi-src" "$ROOT/usr/sbin/policy-rc.d"
rm -f "$ROOT/etc/resolv.conf"
mv "$ROOT/etc/resolv.conf.onvipi" "$ROOT/etc/resolv.conf" 2>/dev/null || true
# Regenerated per device: machine-id (ONVIF device ID!) and SSH host keys
: >"$ROOT/etc/machine-id"
rm -f "$ROOT/var/lib/dbus/machine-id" && ln -s /etc/machine-id "$ROOT/var/lib/dbus/machine-id"
rm -f "$ROOT"/etc/ssh/ssh_host_* "$ROOT/root/.bash_history"
find "$ROOT/var/log" -type f -exec truncate -s 0 {} +

for m in "$ROOT/dev/pts" "$ROOT/dev" "$ROOT/proc" "$ROOT/sys" "$BOOT" "$ROOT"; do
  umount "$m"
done

# ------------------------------------------------------------------ shrink
say "Shrinking file system"
fsck_ok "${LOOP}p2"
resize2fs -M "${LOOP}p2"
BSIZE="$(dumpe2fs -h "${LOOP}p2" 2>/dev/null | awk -F: '/^Block size/{gsub(/ /,"",$2);print $2}')"
BLOCKS="$(dumpe2fs -h "${LOOP}p2" 2>/dev/null | awk -F: '/^Block count/{gsub(/ /,"",$2);print $2}')"
FS_BYTES=$(( BLOCKS * BSIZE + HEADROOM_MB * 1024 * 1024 ))
fsck_ok "${LOOP}p2"
resize2fs "${LOOP}p2" "$(( FS_BYTES / 1024 ))K"
command -v zerofree >/dev/null && zerofree "${LOOP}p2"   # better compression
losetup -d "$LOOP"
LOOP=""

START="$(sfdisk -J "$IMG" | jq '.partitiontable.partitions[1].start')"
SECTORS=$(( (FS_BYTES + 511) / 512 ))
echo "${START},${SECTORS}" | sfdisk -N 2 --no-reread -q "$IMG"
truncate -s $(( (START + SECTORS) * 512 )) "$IMG"
echo "Image: $(( $(stat -c %s "$IMG") / 1024 / 1024 )) MB"

say "Compressing"
xz -T0 -6 -c "$IMG" >"$OUT"
rm -f "$IMG"
ls -lh "$OUT"
