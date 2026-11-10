#!/usr/bin/env python3
"""Erkennt die USB-Webcam und schreibt die go2rtc-Konfiguration.

  onvipi_camera.py --detect   Webcam suchen, Format wählen, in onvipi.conf speichern
  onvipi_camera.py --ensure   nur neu erkennen, wenn die eingestellte Webcam fehlt/nicht passt
  onvipi_camera.py --render   /etc/onvipi/go2rtc.yaml aus onvipi.conf erzeugen
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from onvipilib import CONF, GO2RTC_YAML, STREAM_NAME, load_conf, save_conf, set_group  # noqa: E402

FOURCC = {"MJPG": "mjpeg", "YUYV": "yuyv422", "H264": "h264"}
PREFERRED_SIZES = ["1280x720", "1920x1080", "1024x768", "800x600", "640x480"]


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
    by_id = sorted(glob.glob("/dev/v4l/by-id/*-video-index0"))
    if by_id:
        return by_id
    # Fallback: alle Capture-Geräte (ohne Pi-interne Codec-Geräte /dev/video1x/2x/3x)
    return [d for d in sorted(glob.glob("/dev/video*")) if re.fullmatch(r"/dev/video[0-9]", d)]


def camera_label(dev):
    """Lesbarer Name aus dem /dev/v4l/by-id-Pfad, sonst der Gerätepfad."""
    m = re.match(r"usb-(.+?)-video-index\d+$", os.path.basename(dev))
    if not m:
        return dev
    name = re.sub(r"\s+\d+$", "", m.group(1).replace("_", " ").strip())  # Seriennummer weg
    return re.sub(r"\s+", " ", name) or dev


def formats_of(dev):
    """{format: {größe: [fps, …]}} eines Geräts, {} bei Fehler."""
    try:
        out = subprocess.run(["v4l2-ctl", "--device", dev, "--list-formats-ext"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_formats(out)


def list_cameras():
    """Für die Geräteauswahl im Portal: [{dev, label, formats}] je erkannter USB-Kamera."""
    cams = []
    for dev in find_devices():
        formats = formats_of(dev)
        if formats:
            cams.append({"dev": dev, "label": camera_label(dev), "formats": formats})
    return cams


def detect():
    for dev in find_devices():
        formats = formats_of(dev)
        fmt, size = choose(formats)
        if fmt:
            fps_list = formats[fmt].get(size) or []
            fps = min(fps_list[0], 30) if fps_list else None  # höchste native fps, gedeckelt
            return dev, fmt, size, fps
    return None, None, None, None


def needs_detect(conf):
    """Eingestellte Webcam fehlt (z. B. getauscht) oder kann Format/Auflösung nicht mehr."""
    dev = conf.get("VIDEO_DEVICE") or ""
    if not os.path.exists(dev):
        return True
    formats = formats_of(dev)
    if not formats:
        return False  # Gerät antwortet gerade nicht – nichts überschreiben
    return conf.get("VIDEO_SIZE") not in formats.get(conf.get("VIDEO_FORMAT"), {})


ROTATIONS = (0, 90, 180, 270)


def rotation(conf):
    """Drehung im Uhrzeigersinn aus ROTATE (0/90/180/270), ungültig -> 0."""
    try:
        r = int(conf.get("ROTATE", "0") or 0)
    except ValueError:
        return 0
    return r if r in ROTATIONS else 0


def output_size(conf):
    """Breite, Höhe des Videos nach der Drehung (Hochformat tauscht beides)."""
    w, _, h = (conf.get("VIDEO_SIZE") or "1280x720").partition("x")
    w, h = w or "1280", h or "720"
    return (h, w) if rotation(conf) in (90, 270) else (w, h)


FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def time_overlay(conf):
    """ffmpeg-drawtext: Datum + Uhrzeit (Ortszeit des Pi) unten rechts, halbtransparent hinterlegt."""
    w, h = output_size(conf)
    size = max(12, min(int(w), int(h)) // 28)  # 720p -> 25 px, wächst mit der Auflösung
    margin = size * 2 // 3
    return (f"fontfile={FONT}:fontsize={size}:fontcolor=white"
            f":box=1:boxcolor=black@0.5:boxborderw={size // 4}"
            f":x=w-tw-{margin}:y=h-th-{margin}"
            r":text='%{localtime\:%d.%m.%Y %X}'")  # %X = HH:MM:SS, ':' im Format wäre zu escapen


def render(conf):
    dev = conf.get("VIDEO_DEVICE") or "/dev/video0"
    fmt = conf.get("VIDEO_FORMAT") or "mjpeg"
    size = conf.get("VIDEO_SIZE") or "1280x720"
    fps = conf.get("FRAMERATE") or "15"
    bitrate = conf.get("BITRATE") or "2500k"
    hw = conf.get("HW_ENCODE", "1") == "1"
    rotate = rotation(conf)
    overlay = conf.get("OVERLAY_TIME", "1") == "1"
    query = f"video={dev}&input_format={fmt}&video_size={size}&framerate={fps}"

    if fmt == "h264" and not rotate and not overlay:
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
        source += "#drawtext=" + time_overlay(conf)  # go2rtc zeichnet nach der Drehung

    cfg = {
        "api": {"listen": ":1984"},
        "rtsp": {"listen": ":8554",
                 "username": conf.get("CAM_USER", "admin"),
                 "password": conf.get("CAM_PASSWORD", "")},
        "webrtc": {"listen": ":8555"},
        "ffmpeg": {
            # Pi-4-Hardware-Encoder: Pixelformat und Bitrate sind wichtig für ein sauberes Bild
            "h264/v4l2m2m": f"-c:v h264_v4l2m2m -g {int(fps) * 2} -bf 0 -b:v {bitrate} -pix_fmt:v yuv420p",
        },
        "streams": {STREAM_NAME: source},
        "log": {"level": "info"},
    }
    if not cfg["rtsp"]["password"]:
        del cfg["rtsp"]["username"], cfg["rtsp"]["password"]

    # JSON ist gültiges YAML – so gibt es keine Quoting-Probleme mit Passwörtern
    tmp = GO2RTC_YAML + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# Generated from onvipi.conf – please make changes there\n")
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o640)
    set_group(tmp)
    os.replace(tmp, GO2RTC_YAML)
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--detect", action="store_true")
    ap.add_argument("--ensure", action="store_true")
    ap.add_argument("--render", action="store_true")
    args = ap.parse_args()
    conf = load_conf(CONF)

    if args.ensure and not args.detect and needs_detect(conf):
        print(f"Webcam {conf.get('VIDEO_DEVICE') or '?'} not available – detecting again")
        args.detect = True
    if args.detect:
        dev, fmt, size, fps = detect()
        if not dev:
            print("WARNING: no USB webcam found – keeping the current settings.")
        else:
            info = f"Webcam: {dev}  format: {fmt}  resolution: {size}"
            conf.update(VIDEO_DEVICE=dev, VIDEO_FORMAT=fmt, VIDEO_SIZE=size)
            if fps:
                conf["FRAMERATE"] = str(fps)
                info += f"  frame rate: {fps}"
            print(info)
            save_conf(CONF, conf)
    if args.render:
        render(conf)


if __name__ == "__main__":
    main()
