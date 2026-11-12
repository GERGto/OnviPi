#!/usr/bin/env python3
"""Erkennt die USB-Webcams, verwaltet die Kameraliste und schreibt die go2rtc-Konfiguration.

  onvipi_camera.py --detect   alle Webcams suchen und als Kameras anlegen (neue Installation)
  onvipi_camera.py --ensure   Kameraliste prüfen: fehlende/getauschte Webcam neu zuordnen
  onvipi_camera.py --render   /etc/onvipi/go2rtc.yaml aus der Kameraliste erzeugen

Jede Kamera ist ein Eintrag in /etc/onvipi/cameras.json und erscheint im Netz als
eigenes ONVIF-Gerät (eigener Port, eigene ID) – so bekommt Home Assistant pro Webcam
ein Gerät mit Livebild und Bewegungssensor.

  id           Name des go2rtc-Streams und ONVIF-Profil-Token ("onvipi", "cam2", …)
  name         Anzeigename (ONVIF-Gerätename in Home Assistant)
  enabled      Stream + ONVIF-Gerät aktiv
  device       /dev/v4l/by-id/… (bleibt beim Umstecken gleich)
  format/size/fps, rotate, overlay   Bildquelle und Darstellung
  motion, sensitivity                Bewegungserkennung
  motion_zones   Bereiche, in denen Bewegung zählt (leer = ganzes Bild)
  privacy        Privatzonen – werden im Bild geschwärzt (auch für Aufnahmen)
  audio          Ton vom Mikrofon der Webcam (Opus)
  substream      zusätzlicher Stream "<id>_sub" in kleiner Auflösung (zweites ONVIF-Profil)
  Zonen sind Rechtecke {x, y, w, h} mit Werten 0–1 relativ zum (gedrehten) Bild.
  port         Port des ONVIF-Geräts
  mac          ID des Geräts in Home Assistant – einmal vergeben, danach unverändert
"""
import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from onvipilib import (CAMERAS, CONF, GO2RTC_YAML, STREAM_NAME, builtin_mac,  # noqa: E402
                       device_id, load_conf, load_json, primary_mac, save_json, set_group)

FOURCC = {"MJPG": "mjpeg", "YUYV": "yuyv422", "H264": "h264"}
PREFERRED_SIZES = ["1280x720", "1920x1080", "1024x768", "800x600", "640x480"]
ROTATIONS = (0, 90, 180, 270)
MAX_CAMERAS = 4
MAX_ZONES = 8
SUB_WIDTH = 640     # Breite des Zweitstreams
STREAM_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "onvipi_stream.py")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


# --------------------------------------------------------------------------- Erkennung
def parse_formats(text):
    """Wertet `v4l2-ctl --list-formats-ext` aus -> {format: {größe: [fps, …] absteigend}}."""
    formats, current, size = {}, None, None
    for line in text.splitlines():
        m = re.search(r"\[\d+\]: '(\w+)'", line)
        if m:
            current, size = FOURCC.get(m.group(1)), None
            if current:
                formats.setdefault(current, {})
            continue
        m = re.search(r"Size: \w+ (\d+x\d+)", line)
        if m and current:
            size = m.group(1)
            formats[current].setdefault(size, [])
            continue
        m = re.search(r"Interval:.*\(([\d.]+)\s*fps\)", line)
        if m and current and size:
            fps = int(round(float(m.group(1))))
            if fps and fps not in formats[current][size]:
                formats[current][size].append(fps)
    for sizes in formats.values():
        for s in sizes:
            sizes[s].sort(reverse=True)
    return formats


def choose(formats):
    """Wählt Format/Auflösung: MJPEG 720p bevorzugt (wenig USB-Bandbreite, kameraeigenes Format)."""
    for fmt in ("h264", "mjpeg"):
        sizes = formats.get(fmt)
        if sizes:
            for size in PREFERRED_SIZES:
                if size in sizes:
                    return fmt, size
            return fmt, next(iter(sizes))
    if formats.get("yuyv422"):
        sizes = formats["yuyv422"]
        # Rohformat über USB 2.0 nur in kleiner Auflösung sinnvoll
        return "yuyv422", "640x480" if "640x480" in sizes else list(sizes)[-1]
    return None, None


def find_devices():
    """Stabile Gerätepfade aller USB-Kameras.

    by-id zuerst (Hersteller + Seriennummer). Zwei baugleiche Webcams ohne Seriennummer
    teilen sich einen by-id-Namen – die zweite kommt dann über by-path (USB-Anschluss).
    """
    devs, seen = [], set()
    for pattern, usb_only in (("/dev/v4l/by-id/*-video-index0", False),
                              ("/dev/v4l/by-path/*-video-index0", True)):
        for dev in sorted(glob.glob(pattern)):
            if usb_only and "usb" not in os.path.basename(dev):
                continue  # Pi-interne Codec-/CSI-Geräte
            real = os.path.realpath(dev)
            if real not in seen:
                seen.add(real)
                devs.append(dev)
    if devs:
        return devs
    # Fallback: alle Capture-Geräte (ohne Pi-interne Codec-Geräte /dev/video1x/2x/3x)
    return [d for d in sorted(glob.glob("/dev/video*")) if re.fullmatch(r"/dev/video[0-9]", d)]


def camera_label(dev):
    """Lesbarer Name: aus dem by-id-Pfad, sonst der Name, den der Treiber meldet."""
    m = re.match(r"usb-(.+?)-video-index\d+$", os.path.basename(dev))
    if m and "/by-id/" in dev:
        name = re.sub(r"\s+\d+$", "", m.group(1).replace("_", " ").strip())  # Seriennummer weg
        name = re.sub(r"\s+", " ", name)
        if name:
            return name
    try:
        node = os.path.basename(os.path.realpath(dev))
        with open(f"/sys/class/video4linux/{node}/name", encoding="utf-8") as f:
            return f.read().strip().split(":")[0] or dev
    except OSError:
        return dev


def formats_of(dev):
    """{format: {größe: [fps, …]}} eines Geräts, {} bei Fehler."""
    try:
        out = subprocess.run(["v4l2-ctl", "--device", dev, "--list-formats-ext"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_formats(out)


def microphone_of(dev):
    """ALSA-Gerät des Mikrofons derselben USB-Webcam, z. B. 'plughw:CARD=C920,DEV=0' (oder None)."""
    try:
        node = os.path.basename(os.path.realpath(dev))
        iface = os.path.realpath(f"/sys/class/video4linux/{node}/device")
        for card in sorted(glob.glob(os.path.join(os.path.dirname(iface), "*", "sound", "card*"))):
            if not glob.glob(os.path.join(card, "pcm*c")):
                continue  # nur Wiedergabe, kein Aufnahmegerät
            with open(os.path.join(card, "id"), encoding="utf-8") as f:
                return f"plughw:CARD={f.read().strip()},DEV=0"
    except OSError:
        pass
    return None


def list_devices():
    """[{dev, label, formats, mic}] je angeschlossener USB-Kamera."""
    found = []
    for dev in find_devices():
        formats = formats_of(dev)
        if formats:
            found.append({"dev": dev, "label": camera_label(dev), "formats": formats,
                          "mic": bool(microphone_of(dev))})
    return found


# --------------------------------------------------------------------------- Kameraliste
def synthetic_mac(cam_id):
    """Feste, lokal verwaltete MAC für zusätzliche Kameras (HA unterscheidet Geräte daran)."""
    b = bytearray(hashlib.sha1(f"{device_id()}:{cam_id}".encode()).digest()[:6])
    b[0] = (b[0] & 0xFC) | 0x02  # lokal verwaltet, keine Gruppenadresse
    return ":".join(f"{x:02x}" for x in b)


def camera_uuid(cam):
    """ONVIF-Endpunkt-ID. Die erste Kamera behält die bisherige Geräte-ID."""
    if cam["id"] == STREAM_NAME:
        return device_id()
    return str(uuid.uuid5(uuid.UUID(device_id()), cam["id"]))


def effective_mac(cam):
    """MAC, unter der Home Assistant die Kamera kennt (bis zur Vergabe: aktive Schnittstelle)."""
    return cam.get("mac") or primary_mac()


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clean_zones(zones):
    """Gültige Rechtecke {x, y, w, h} (0–1, innerhalb des Bildes), höchstens MAX_ZONES."""
    out = []
    for z in zones if isinstance(zones, list) else []:
        try:
            x, y = max(0.0, min(1.0, float(z["x"]))), max(0.0, min(1.0, float(z["y"])))
            w, h = min(float(z["w"]), 1.0 - x), min(float(z["h"]), 1.0 - y)
        except (KeyError, TypeError, ValueError):
            continue
        if w >= 0.02 and h >= 0.02:
            out.append({"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)})
    return out[:MAX_ZONES]


def normalize(cam):
    """Füllt fehlende Felder und begrenzt Werte – die Datei darf von Hand bearbeitet werden.

    Gerät, Format und Auflösung landen in der go2rtc-Quelle bzw. im ffmpeg-Aufruf. Sie werden
    deshalb streng geprüft, auch wenn sie aus einer eingespielten Sicherung stammen.
    """
    rot = _int(cam.get("rotate"), 0)
    cam_id = str(cam.get("id") or STREAM_NAME)
    device = str(cam.get("device") or "")
    fmt, size = str(cam.get("format") or "mjpeg"), str(cam.get("size") or "1280x720")
    return {
        "id": cam_id if re.fullmatch(r"[a-z0-9]{1,16}", cam_id) else STREAM_NAME,
        "name": (re.sub(r"[\x00-\x1f'\"\\`$<>&]", "", str(cam.get("name") or "")).strip()
                 or "OnviPi")[:48],
        "enabled": bool(cam.get("enabled", True)),
        "device": device if re.fullmatch(r"/dev/[A-Za-z0-9_./:+-]{1,200}", device)
        and ".." not in device else "",
        "format": fmt if fmt in FOURCC.values() else "mjpeg",
        "size": size if re.fullmatch(r"\d{2,5}x\d{2,5}", size) else "1280x720",
        "fps": max(1, min(60, _int(cam.get("fps"), 15))),
        "rotate": rot if rot in ROTATIONS else 0,
        "overlay": bool(cam.get("overlay", True)),
        "motion": bool(cam.get("motion", True)),
        "sensitivity": max(0, min(100, _int(cam.get("sensitivity"), 60))),
        "motion_zones": clean_zones(cam.get("motion_zones")),
        "privacy": clean_zones(cam.get("privacy")),
        "audio": bool(cam.get("audio", False)),
        "substream": bool(cam.get("substream", False)),
        "port": _int(cam.get("port"), 8000),
        "mac": cam.get("mac") or None,
    }


def legacy_camera(conf):
    """Kamera aus den früheren Einzelwerten in onvipi.conf (Installationen vor 2.1)."""
    return normalize({
        "id": STREAM_NAME, "name": conf.get("CAM_NAME"), "device": conf.get("VIDEO_DEVICE"),
        "format": conf.get("VIDEO_FORMAT"), "size": conf.get("VIDEO_SIZE"),
        "fps": conf.get("FRAMERATE"), "rotate": conf.get("ROTATE"),
        "overlay": conf.get("OVERLAY_TIME", "1") == "1",
        "motion": conf.get("MOTION_ENABLE", "1") == "1",
        "sensitivity": conf.get("MOTION_SENSITIVITY"),
        "port": conf.get("ONVIF_PORT"), "mac": None,
    })


def load_cameras(conf=None):
    """Kameraliste; ohne cameras.json die eine Kamera aus onvipi.conf (wird nicht gespeichert)."""
    data = load_json(CAMERAS)
    if isinstance(data, dict) and isinstance(data.get("cameras"), list):
        return [normalize(c) for c in data["cameras"] if isinstance(c, dict)]
    conf = load_conf(CONF) if conf is None else conf
    return [legacy_camera(conf)] if conf.get("VIDEO_DEVICE") else []


def save_cameras(cams):
    save_json(CAMERAS, {"cameras": [normalize(c) for c in cams]})


def get_camera(cams, cam_id):
    return next((c for c in cams if c["id"] == cam_id), None)


def new_camera(cams, device, conf=None):
    """Legt für ein erkanntes Gerät ({dev, label, formats}) einen Eintrag mit freier ID/Port an."""
    conf = load_conf(CONF) if conf is None else conf
    ids = {c["id"] for c in cams}
    ports = {c["port"] for c in cams}
    if STREAM_NAME not in ids:
        cam_id = STREAM_NAME
    else:
        cam_id = next(f"cam{n}" for n in range(2, 100) if f"cam{n}" not in ids)
    port = _int(conf.get("ONVIF_PORT"), 8000)
    while port in ports:
        port += 1
    fmt, size = choose(device["formats"])
    fps_list = device["formats"].get(fmt, {}).get(size) or []
    first = cam_id == STREAM_NAME
    return normalize({
        "id": cam_id, "device": device["dev"], "format": fmt, "size": size,
        "name": conf.get("CAM_NAME") if first else device["label"],
        "fps": min(fps_list[0], 30) if fps_list else 15,  # höchste native fps, gedeckelt
        "port": port, "mac": None if first else synthetic_mac(cam_id),
    })


def _same(dev_a, dev_b):
    return bool(dev_a and dev_b) and os.path.realpath(dev_a) == os.path.realpath(dev_b)


def reconcile(cams, devices):
    """Gleicht die Liste mit den angeschlossenen Geräten ab. Gibt True zurück, wenn geändert.

    * Genau eine Kamera fehlt und genau ein Gerät ist frei -> Webcam wurde getauscht.
    * Gerät kann das eingestellte Format nicht mehr -> neu wählen.
    Mehrdeutige Fälle bleiben unverändert (die Oberfläche zeigt sie als "nicht verbunden").
    """
    changed = False
    missing = [c for c in cams if not any(_same(c["device"], d["dev"]) for d in devices)]
    free = [d for d in devices if not any(_same(c["device"], d["dev"]) for c in cams)]
    if len(missing) == 1 and len(free) == 1 and devices:
        missing[0]["device"] = free[0]["dev"]
        changed = True
    for cam in cams:
        dev = next((d for d in devices if _same(cam["device"], d["dev"])), None)
        if not dev or cam["size"] in dev["formats"].get(cam["format"], {}):
            continue
        fmt, size = choose(dev["formats"])
        if fmt:
            fps_list = dev["formats"][fmt].get(size) or []
            cam.update(format=fmt, size=size, fps=min(fps_list[0], 30) if fps_list else cam["fps"])
            changed = True
    return changed


def pin_mac(cams, fresh=False):
    """Vergibt der ersten Kamera einmalig ihre MAC. Gibt True zurück, wenn geändert.

    Neue Installation: MAC der fest verbauten Schnittstelle. Bestehende Installation:
    die MAC der gerade aktiven Schnittstelle – darunter kennt Home Assistant das Gerät.
    """
    changed = False
    for cam in cams:
        if cam.get("mac"):
            continue
        if cam["id"] == STREAM_NAME:
            mac = builtin_mac() if fresh else primary_mac()
        else:
            mac = synthetic_mac(cam["id"])
        if mac:
            cam["mac"] = mac
            changed = True
    return changed


def overview(cams=None, devices=None):
    """Für die Oberfläche: eingerichtete Kameras (mit Zustand) + neu erkannte Geräte."""
    cams = load_cameras() if cams is None else cams
    devices = list_devices() if devices is None else devices
    items = []
    for cam in cams:
        dev = next((d for d in devices if _same(cam["device"], d["dev"])), None)
        items.append(dict(cam, present=bool(dev), new=False,
                          label=dev["label"] if dev else camera_label(cam["device"]),
                          formats=dev["formats"] if dev else {},
                          mic=bool(dev and dev.get("mic"))))
    known = list(cams)
    for dev in devices:
        if any(_same(c["device"], dev["dev"]) for c in cams):
            continue
        cam = new_camera(known, dev)
        known.append(cam)
        items.append(dict(cam, enabled=False, present=True, new=True,
                          label=dev["label"], formats=dev["formats"], mic=bool(dev.get("mic"))))
    return items


# --------------------------------------------------------------------------- go2rtc
def rotation(cam):
    r = _int(cam.get("rotate"), 0)
    return r if r in ROTATIONS else 0


def output_size(cam):
    """Breite, Höhe des Videos nach der Drehung (Hochformat tauscht beides)."""
    w, _, h = (cam.get("size") or "1280x720").partition("x")
    w, h = w or "1280", h or "720"
    return (h, w) if rotation(cam) in (90, 270) else (w, h)


def time_overlay(cam):
    """ffmpeg-drawtext: Datum + Uhrzeit (Ortszeit des Pi) unten rechts, halbtransparent hinterlegt."""
    w, h = output_size(cam)
    size = max(12, min(int(w), int(h)) // 28)  # 720p -> 25 px, wächst mit der Auflösung
    margin = size * 2 // 3
    return (f"fontfile={FONT}:fontsize={size}:fontcolor=white"
            f":box=1:boxcolor=black@0.5:boxborderw={size // 4}"
            f":x=w-tw-{margin}:y=h-th-{margin}"
            r":text='%{localtime\:%d.%m.%Y %X}'")  # %X = HH:MM:SS, ':' im Format wäre zu escapen


def sub_size(cam):
    """Breite, Höhe des Zweitstreams (gleiches Seitenverhältnis, gerade Werte)."""
    w, h = (int(v) for v in output_size(cam))
    if w <= SUB_WIDTH:
        return w, h
    return SUB_WIDTH, max(2, int(round(SUB_WIDTH * h / w / 2)) * 2)


def sub_id(cam):
    return cam["id"] + "_sub"


def stream_source(cam, hw):
    if cam.get("privacy"):
        # Privatzonen müssen VOR dem Kodieren ins Bild – das kann go2rtcs ffmpeg-Vorlage
        # nicht, deshalb baut onvipi_stream.py den ffmpeg-Aufruf selbst (immer Software).
        return f"exec:/usr/bin/python3 {STREAM_HELPER} video {cam['id']} {{output}}"
    query = (f"video={cam['device'] or '/dev/video0'}&input_format={cam['format']}"
             f"&video_size={cam['size']}&framerate={cam['fps']}")
    rotate, overlay = rotation(cam), cam.get("overlay")
    if cam["format"] == "h264" and not rotate and not overlay:
        source = f"v4l2:device?{query}"          # Kamera liefert schon H.264
    elif hw:
        # Hardware-Encoder (v4l2m2m) spart CPU, erzeugt aber einen H264-Bitstrom, an dem
        # go2rtcs WebRTC-/MSE-Player nach dem ersten Keyframe einfrieren (nur RTSP läuft).
        # Deshalb NICHT Standard – nur setzen, wenn die CPU-Last eng wird und RTSP reicht.
        source = f"ffmpeg:device?{query}#video=h264#hardware=v4l2m2m"
    else:
        source = f"ffmpeg:device?{query}#video=h264"  # Software (libx264), ~1 Kern bei 720p30
    if rotate:
        source += f"#rotate={rotate}"  # go2rtc dreht per ffmpeg-transpose (H.264-Kameras: neu kodiert)
    if overlay:
        source += "#drawtext=" + time_overlay(cam)  # go2rtc zeichnet nach der Drehung
    return source


def render(conf, cams):
    bitrate = conf.get("BITRATE") or "2500k"
    hw = conf.get("HW_ENCODE", "1") == "1"
    active = [c for c in cams if c["enabled"]]
    gop = (active[0]["fps"] if active else 15) * 2

    cfg = {
        # go2rtcs API hat kein Passwort und kann Streams mit beliebigen Befehlen anlegen –
        # deshalb nur lokal. Livebild und Standbilder reichen onvipi_web (/live/) und
        # onvipi_onvif (/snapshot.jpg) mit Anmeldung durch. GO2RTC_LAN='1' öffnet sie wieder.
        "api": {"listen": ":1984" if conf.get("GO2RTC_LAN") == "1" else "127.0.0.1:1984"},
        "rtsp": {"listen": ":8554",
                 "username": conf.get("CAM_USER", "admin"),
                 "password": conf.get("CAM_PASSWORD", "")},
        "webrtc": {"listen": ":8555"},
        "ffmpeg": {
            # Pi-4-Hardware-Encoder: Pixelformat und Bitrate sind wichtig für ein sauberes Bild
            "h264/v4l2m2m": f"-c:v h264_v4l2m2m -g {gop} -bf 0 -b:v {bitrate} -pix_fmt:v yuv420p",
        },
        "streams": {},
        "log": {"level": "info"},
    }
    for c in active:
        sources = [stream_source(c, hw)]
        if c.get("audio") and microphone_of(c["device"]):
            # zweite Quelle = Ton; go2rtc führt Video und Ton zu einem Stream zusammen
            sources.append(f"exec:/usr/bin/python3 {STREAM_HELPER} audio {c['id']} {{output}}")
        cfg["streams"][c["id"]] = sources if len(sources) > 1 else sources[0]
        if c.get("substream"):
            w, h = sub_size(c)
            # läuft nur, solange ihn jemand abruft (neu kodiert aus dem Hauptstream)
            cfg["streams"][sub_id(c)] = (
                f"ffmpeg:{c['id']}#video=h264#width={w}#height={h}"
                + ("#audio=copy" if len(sources) > 1 else ""))
    if not cfg["rtsp"]["password"]:
        del cfg["rtsp"]["username"], cfg["rtsp"]["password"]

    # JSON ist gültiges YAML – so gibt es keine Quoting-Probleme mit Passwörtern
    tmp = GO2RTC_YAML + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# Generated from onvipi.conf and cameras.json – please make changes there\n")
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o640)
    set_group(tmp)
    os.replace(tmp, GO2RTC_YAML)
    return cfg


# --------------------------------------------------------------------------- Befehle
def detect(conf):
    """Neue Installation: jede angeschlossene Webcam wird eine aktive Kamera."""
    cams = []
    for dev in list_devices()[:MAX_CAMERAS]:
        cam = new_camera(cams, dev, conf)
        cams.append(cam)
        print(f"Webcam: {cam['device']}  format: {cam['format']}  "
              f"resolution: {cam['size']}  frame rate: {cam['fps']}")
    if not cams:
        print("WARNING: no USB webcam found – plug one in and enable it on the settings page.")
    pin_mac(cams, fresh=True)
    save_cameras(cams)
    return cams


def ensure(conf):
    """Bei jedem Start: Liste anlegen (Update von < 2.1), Webcam-Tausch erkennen, MAC vergeben."""
    stored = os.path.exists(CAMERAS)
    cams = load_cameras(conf)
    devices = list_devices()
    changed = not stored
    if not cams and devices:   # bisher keine Webcam gefunden, jetzt steckt eine
        cams = [new_camera([], devices[0], conf)]
        changed = True
    if reconcile(cams, devices):
        print("Webcam changed – camera list updated")
        changed = True
    changed = pin_mac(cams) or changed
    if changed:
        save_cameras(cams)
    return cams


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--detect", action="store_true")
    ap.add_argument("--ensure", action="store_true")
    ap.add_argument("--render", action="store_true")
    args = ap.parse_args()
    conf = load_conf(CONF)

    cams = None
    if args.detect:
        cams = detect(conf)
    elif args.ensure:
        cams = ensure(conf)
    if args.render:
        render(conf, load_cameras(conf) if cams is None else cams)


if __name__ == "__main__":
    main()
