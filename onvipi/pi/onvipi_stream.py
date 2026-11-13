#!/usr/bin/env python3
"""Startet ffmpeg für Quellen, die go2rtcs eigene ffmpeg-Vorlagen nicht abdecken.

go2rtc ruft dieses Skript als "exec:"-Quelle auf und gibt die RTSP-Adresse mit, an die
ffmpeg liefern soll:

  onvipi_stream.py video <kamera-id> <rtsp-ziel>   Bild mit geschwärzten Privatzonen
  onvipi_stream.py audio <kamera-id> <rtsp-ziel>   Ton vom Mikrofon der Webcam (Opus)

Das Skript ersetzt sich selbst durch ffmpeg (exec) – go2rtc beendet damit direkt ffmpeg.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from onvipilib import CONF, load_conf  # noqa: E402
from onvipi_camera import (FONT, get_camera, load_cameras, microphone_of, output_size,  # noqa: E402
                           rotation)

TRANSPOSE = {90: ["transpose=1"], 180: ["transpose=1", "transpose=1"], 270: ["transpose=2"]}


def mask_pixels(zone, width, height):
    """Pixelgenaue Ecken (x0, y0, x1, y1) einer Privatzone.

    Nach außen auf gerade Pixel gerundet (die Farbe ist im Video nur halb so fein aufgelöst –
    sonst bleibt ein farbiger Saum), und was fast bis zum Rand reicht, reicht bis zum Rand.
    """
    def span(start, size, total):
        a = int(start * total) // 2 * 2
        b = min(total, -(-int(round((start + size) * total)) // 2) * 2)
        if a <= total * 0.01:
            a = 0
        if b >= total * 0.99:
            b = total
        return a, max(b, a + 2)
    x0, x1 = span(zone["x"], zone["w"], width)
    y0, y1 = span(zone["y"], zone["h"], height)
    return x0, y0, x1, y1


def video_filters(cam):
    """Filterkette: drehen -> Privatzonen schwärzen -> Datum/Uhrzeit."""
    filters = list(TRANSPOSE.get(rotation(cam), []))
    width, height = (int(v) for v in output_size(cam))
    for z in cam.get("privacy", []):
        x0, y0, x1, y1 = mask_pixels(z, width, height)
        filters.append(f"drawbox=x={x0}:y={y0}:w={x1 - x0}:h={y1 - y0}:color=black:t=fill")
    if cam.get("overlay"):
        w, h = output_size(cam)
        size = max(12, min(int(w), int(h)) // 28)
        margin = size * 2 // 3
        filters.append(
            f"drawtext=fontfile={FONT}:fontsize={size}:fontcolor=white"
            f":box=1:boxcolor=black@0.5:boxborderw={size // 4}"
            f":x=w-tw-{margin}:y=h-th-{margin}"
            r":text='%{localtime\:%d.%m.%Y %X}'")
    return filters


def video_command(cam, conf, output):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-f", "v4l2", "-input_format", cam["format"], "-video_size", cam["size"],
           "-framerate", str(cam["fps"]), "-i", cam["device"] or "/dev/video0", "-an"]
    filters = video_filters(cam)
    if filters:
        cmd += ["-vf", ",".join(filters)]
    bitrate = conf.get("BITRATE") or "2500k"
    cmd += ["-c:v", "libx264", "-preset:v", "superfast", "-tune:v", "zerolatency",
            "-profile:v", "high", "-level:v", "4.1", "-pix_fmt:v", "yuv420p",
            "-g", str(cam["fps"] * 2), "-b:v", bitrate, "-maxrate", bitrate,
            "-bufsize", bitrate, "-rtsp_transport", "tcp", "-f", "rtsp", output]
    return cmd


def audio_command(cam, output):
    mic = microphone_of(cam["device"])
    if not mic:
        return None
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "alsa", "-i", mic, "-vn",
            "-c:a", "libopus", "-ac", "1", "-ar", "48000", "-b:a", "32k",
            "-application", "lowdelay", "-rtsp_transport", "tcp", "-f", "rtsp", output]


def main(argv):
    if len(argv) != 3 or argv[0] not in ("video", "audio"):
        print(__doc__, file=sys.stderr)
        return 2
    conf = load_conf(CONF)
    cam = get_camera(load_cameras(conf), argv[1])
    if not cam:
        print(f"unknown camera {argv[1]}", file=sys.stderr)
        return 1
    cmd = video_command(cam, conf, argv[2]) if argv[0] == "video" else audio_command(cam, argv[2])
    if not cmd:
        print("no microphone found for this camera", file=sys.stderr)
        return 1
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
