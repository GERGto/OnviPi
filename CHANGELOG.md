# Changelog

All notable changes to OnviPi. Downloads are available under
[Releases](https://github.com/GERGto/OnviPi/releases).

## [2.0] – 2026-11-10

### Changed
- **New name: OnviPi** (formerly PiCam). The ready-to-flash image is now called **OnviPiOS**
  (`onvipios-arm64.img.xz`), the package `onvipi.tar.gz`.
- Services, paths and commands renamed: `onvipi-*` services, `/etc/onvipi/onvipi.conf`,
  `/opt/onvipi`, command `onvipi-wifi`, hostname `onvipi-<name>.local`, hotspot
  `OnviPi-Setup-XXXX`, RTSP path `/onvipi`.
- **English user interface:** setup wizard, settings page, Home Assistant entity names,
  installer output and log messages. The documentation is in English as well.

### Added
- **Automatic migration** of existing PiCam installations by `install.sh`: settings, camera
  password and Wi-Fi networks are kept; the ONVIF device ID stays the same, so Home Assistant
  keeps the device.
- Screenshots and system requirements in the README.

## [1.1] – 2026-11-10

### Added
- **Ready-to-flash image** based on Raspberry Pi OS Lite 64-bit: flash, boot, set up from your
  phone – no SSH and no Imager customisation needed.
- **“Access” step in the setup wizard:** user and password for the Pi, SSH on/off, Wi-Fi
  country and time zone.
- Without a configured user the hotspot starts immediately, even on Ethernet.
- **SSH on/off** on the settings page.
- The hostname follows the camera name from the wizard.
- **One-line installation** of the package.
- Automatic builds and releases with GitHub Actions (native arm64 runner).

### Changed
- `install.sh` is split into a device-independent part (`setup/base.sh`) and a
  device-specific part (`setup/firstboot.sh`) – image and package use the same scripts.
  Running it again updates the installation and keeps the settings.
- go2rtc pinned to a fixed version (1.9.9) instead of “latest”.
- The webcam is checked on every start and only detected again if the configured one is
  missing or no longer fits – your own settings are kept.
- Hostname and hotspot name contain an ID from the MAC address, so several cameras on the
  same network can be told apart.

### Security
- The image contains no shared secrets: camera password, SSH host keys and `machine-id` are
  generated per device on first boot.
- A login user can only be created on the open hotspot while none exists.

## [1.0]

- First version (as PiCam): ONVIF camera with live view and motion detection for Home
  Assistant, setup hotspot, settings page, optional MQTT diagnostics.
