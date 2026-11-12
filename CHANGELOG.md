# Changelog

All notable changes to OnviPi. Downloads are available under
[Releases](https://github.com/GERGto/OnviPi/releases).

## [2.1] – 2026-11-12

### Added
- **Several webcams on one Pi.** Each webcam is its own ONVIF device (own port, ID, stream and
  motion detection) and appears in Home Assistant as a separate camera.
- **New web interface** for phone and desktop with login page and navigation: Overview,
  Cameras, Network, Home Assistant, System. **Expert mode** adds fixed IP, hostname, SSH, MQTT
  and stream addresses.
- **Local recording** to a USB stick/SSD or the internal SD card: when something moves (with
  lead-in) or all the time, ring buffer by days and free space. **Recordings gallery** in the
  web interface with preview pictures: watch, download, delete single clips, several or a day.
- **Motion areas** (movement only counts inside drawn rectangles) and **privacy masks** (blacked
  out in live view, Home Assistant and recordings), both drawn directly on the picture.
- **Sound** from the webcam's microphone (optional, per camera).
- **Extra low-resolution stream** per camera as a second ONVIF profile.
- **Update with one click** from the latest GitHub release (checksum verified).
- **Backup / restore** of all settings as one file, and **factory reset**.
- **Terminal in the browser** (ttyd, expert mode): off by default, switched on for a limited
  time only.
- **Network settings:** choose the Wi-Fi adapter (USB adapters with external antenna), connect
  to another Wi-Fi from the settings page, DHCP or fixed IP with automatic revert if the
  device becomes unreachable.
- **Home Assistant buttons work:** *Reboot* restarts the Pi, *Set system date and time* sets
  the clock when no NTP time is available. The *Visit* link opens the settings page.
- Warnings for undervoltage, heat, full SD card and missing cameras; log download.
- Password reset with a file `onvipi-reset` on the SD card's boot partition.
- `tools/dev-server.py` runs the web interface with a simulated device for development.

### Changed
- Raspberry Pi 5 is now a tested platform.
- **Setup wizard reworked:** only the essentials are asked (camera name, password, network).
  The access details are shown *before* anything changes and must be confirmed; a wrong Wi-Fi
  password brings the wizard back at the network step with the other answers kept.
- **One account** instead of two: the password chosen in the wizard is used for the settings
  page, ONVIF/RTSP and the login on the Pi. SSH is no longer asked for in the wizard.
- **On Ethernet no Wi-Fi is required**, and the wizard is also reachable over the LAN.
- The hotspot of an already set-up device shows the settings page with login instead of the
  open wizard.
- The ONVIF device ID no longer depends on the active network interface.
- With MQTT enabled, the diagnostic sensors join the camera's device in Home Assistant.
- Camera settings moved from `onvipi.conf` to `cameras.json` (converted automatically; Home
  Assistant keeps the existing device).

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
