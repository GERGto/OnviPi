# Changelog

All notable changes to OnviPi. Downloads are available under
[Releases](https://github.com/GERGto/OnviPi/releases).

## [2.5] – 2026-11-13

### Fixed
- **Browser terminal stayed blank** (only a cursor). It started `login`, which briefly
  disconnects the terminal while starting; the terminal now opens a shell of your OnviPi
  account directly – you are already signed in to the web interface. Messages of the terminal
  program now go to the log.

### Added
- *System → Remote access* shows the SSH fingerprint of the device, with a hint for the
  “host identification has changed” warning after re-flashing.

## [2.4] – 2026-11-13

First release with feedback from real devices (Pi 4 and Pi 5).

### Fixed
- **“Recordings without movement”:** with *When something moves* the gallery also showed the
  minute that was just being written. Clips are now collected in a hidden folder first and
  only appear in the gallery if the camera really saw movement.
- **Setup wizard:** the button on the first page was cut off at the bottom on phones.
- **Terminal:** ttyd was missing on Raspberry Pi OS (no package). The official binary is now
  installed, pinned by version and checksum.
- **Privacy masks** reach the edge of the picture exactly (no thin strip left over).
- Router names such as `http://picam.fritz.box` are accepted again by the web interface.
- Overview: a single camera is as wide as the tiles below; portrait cameras are shown upright
  without black bars. Recordings: selector and buttons have the same height.

### Added
- **Use without Home Assistant:** the page *Connect* (formerly *Home Assistant*) explains the
  three ways – on its own, Home Assistant, other ONVIF/RTSP apps – with addresses to copy
  and a “Connected” indicator for Home Assistant. Wording in wizard and interface no longer
  assumes Home Assistant.
- **Why a clip exists:** every clip shows its movements with time and duration; the overview
  shows the last movement per camera. Movements are logged next to the recordings.
- **Live motion bar** under the sensitivity slider: the movement right now and the trigger level.
- **Motion areas and privacy masks can be moved and resized** (drag, corner handles).
- Tests for camera list, streams, masks, recording and updates run in the release pipeline.

### Changed
- All wizard pages share one layout: title on top, content in cards.
- Motion detection also runs while recording all the time, so those clips show movements too.
- ONVIF reports the real firmware version.

## [2.3] – 2026-11-12

### Security
- **go2rtc's management interface (port 1984) is no longer reachable from the network.** It
  has no password and can start arbitrary commands, so anyone in the home network could have
  taken over the device. Live view and snapshots now go through OnviPi with login.
- **Snapshots need the password** (HTTP Basic or Digest, served on the ONVIF port).
- **The hotspot of a set-up device is WPA2-encrypted** with the account password. Before, the
  login on the open hotspot could be read by anyone in range.
- **Lock-out for ONVIF and snapshots:** 10 wrong passwords lock the address for 5 minutes.
- **DNS-rebinding protection:** the web interface only answers under the device's own
  addresses (IP, `<name>.local`, common router domains, `TRUSTED_HOSTS`).
- Content-Security-Policy for all pages, connection time-outs, size limit for ONVIF requests.
- Camera settings from a restored backup are validated strictly before they reach go2rtc.
- go2rtc and the discovery service can no longer gain privileges or read home directories.

### Added
- **Wizard step “Updates”** with two switches, both on by default: look for updates
  automatically, and install them automatically. The page *Updates* has the same two switches.

### Changed
- README: security section rewritten with the protections and the known limits.

### Upgrade notes
- Home Assistant: choose *Reconfigure* on the ONVIF device once so that it picks up the new
  snapshot address. Live view and motion sensor are not affected.
- If you opened `http://<pi>:1984` directly: use the live view of the web interface, or set
  `GO2RTC_LAN='1'` in `onvipi.conf` to open the port again (not recommended).

## [2.2] – 2026-11-12

### Changed
- **Privacy note:** with automatic updates on, the camera contacts GitHub once a night. Switch
  it off under *Updates* if the device must not connect to the internet on its own.
- README shortened and reorganised.
- **App-like interface on phones.** In the setup wizard the buttons (*Back*, *Next*) sit in a
  fixed bar at the bottom – always at the same place – with a fixed header and step
  transitions. The settings interface has a fixed title bar, dialogs slide up from the
  bottom, and pages no longer bounce or flash on tap.
- **Live view instead of still pictures** in the wizard, on the overview and on the camera
  cards. It runs through the settings page itself (port 80, same login); if the player is not
  available, a picture that refreshes itself is shown.
- The title of a camera card follows the name field while typing.
- **Recording can no longer overfill the storage:** free space is checked every two seconds,
  the oldest recordings go first, a reserve always stays free, and recording pauses if other
  data fills the storage. Without the USB storage nothing is written to the SD card instead.

### Added
- **Automatic updates,** on by default: every night the camera looks for a new release and
  installs it. New page **Updates** with the installed version, the last check and update,
  *Check for updates* and the switch for the automatic. (Replaces the update card under
  *System*.)
- Recording: optional size limit in GB and a display of how many days fit on the storage.
- The settings page can be added to the phone's home screen and then opens like an app
  (icon, no browser bars).

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
