# Changelog

Alle wichtigen Änderungen an PiCam. Downloads unter
[Releases](https://github.com/GERGto/PiCam/releases).

## [1.1] – 2026-11-10

### Neu
- **Fertiges Image** (`picam-arm64.img.xz`) auf Basis von Raspberry Pi OS Lite 64-bit:
  flashen, starten, per Handy einrichten – ohne SSH und ohne Imager-Einstellungen.
- **Schritt „Zugang“ im Einrichtungs-Assistenten:** Benutzer und Passwort für den Pi,
  SSH an/aus, WLAN-Land und Zeitzone.
- Ohne eingerichteten Benutzer startet der Hotspot sofort, auch wenn ein LAN-Kabel steckt.
- **SSH an/aus** auf der Einstellungsseite.
- Der Hostname folgt dem Kameranamen aus dem Assistenten (`picam-<name>.local`).
- **Installation per Einzeiler:** `curl … | tar xz && sudo bash picam/install.sh`.
- Automatische Builds und Releases mit GitHub Actions (nativer arm64-Runner).

### Geändert
- `install.sh` ist in einen geräteunabhängigen Teil (`setup/base.sh`) und einen
  gerätespezifischen Teil (`setup/firstboot.sh`) aufgeteilt – Image und Paket nutzen dieselben
  Skripte. Erneutes Ausführen aktualisiert PiCam und behält die Einstellungen.
- go2rtc in fester Version (1.9.9) statt „latest“.
- Die Webcam wird bei jedem Start geprüft und nur neu erkannt, wenn die eingestellte fehlt
  oder nicht mehr passt – eigene Einstellungen bleiben erhalten.
- Hostname und Hotspot-Name enthalten eine Kennung aus der MAC-Adresse, damit sich mehrere
  PiCams im selben Netz unterscheiden.

### Sicherheit
- Das Image enthält keine gemeinsamen Geheimnisse: Kamera-Passwort, SSH-Hostschlüssel und
  `machine-id` entstehen pro Gerät beim ersten Start.
- Ein Login-Benutzer lässt sich im offenen Hotspot nur anlegen, solange noch keiner existiert.

## [1.0]

- Erste Version: ONVIF-Kamera mit Livebild und Bewegungserkennung für Home Assistant,
  Einrichtungs-Hotspot, Weboberfläche, optionale MQTT-Diagnose.
