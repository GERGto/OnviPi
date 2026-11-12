#!/usr/bin/env python3
"""Aufzeichnung auf USB-Speicher (Stick oder SSD) oder auf die interne SD-Karte.

Einstellungen in onvipi.conf (Seite "Recordings" der Oberfläche):
  REC_MODE   off | motion | continuous
  REC_UUID   Dateisystem-UUID des gewählten USB-Speichers, oder "local" = SD-Karte
  REC_DAYS   Aufnahmen nach so vielen Tagen löschen (Standard 14)
  REC_MAX_GB Aufnahmen belegen höchstens so viele GB (leer/0 = so viel wie frei ist)

Der Speicher kann nicht volllaufen – drei Sicherungen greifen nacheinander:
  1. Ringspeicher: Aufnahmen älter als REC_DAYS, über REC_MAX_GB oder – sobald weniger als
     die Reserve frei ist – die jeweils ältesten werden gelöscht. Geprüft wird der freie
     Platz alle 2 Sekunden.
  2. Notbremse: Ist trotz Löschen weniger als die halbe Reserve frei (andere Daten füllen
     den Speicher), wird die Aufnahme angehalten und erst fortgesetzt, wenn wieder die
     ganze Reserve frei ist.
  3. Gelöscht werden ausschließlich eigene Aufnahmen (JJJJMMTT-HHMMSS.mp4 im Ordner
     onvipi), nie andere Dateien. Fehlt der USB-Speicher, wird nicht ersatzweise auf die
     SD-Karte geschrieben.

Arbeitsweise:
  * Ein USB-Speicher wird unter MOUNT eingehängt (ext4, exFAT, FAT32, NTFS – nichts wird
    formatiert). Aufnahmen liegen in <MOUNT>/onvipi/<kamera-id>/JJJJMMTT-HHMMSS.mp4.
  * "local" schreibt nach LOCAL_DIR auf der SD-Karte des Betriebssystems. Das geht ohne
    Zusatzgerät, nutzt die Karte aber ab und lässt dem System LOCAL_MIN_FREE frei.
  * Je aktiver Kamera kopiert ein ffmpeg den vorhandenen H.264-Stream in Abschnitte
    (kein Neukodieren, kaum CPU). Fragmentiertes MP4: auch nach einem Stromausfall
    abspielbar, läuft direkt im Browser.
  * "motion": Es wird durchgehend in 60-Sekunden-Abschnitten geschrieben und jeder
    Abschnitt ohne Bewegung wieder gelöscht. So ist der Vorlauf vor der Bewegung
    automatisch enthalten. Die Bewegung meldet onvipi_onvif über Marker-Dateien.
  * Ringspeicher: Altes wird nach REC_DAYS gelöscht und zusätzlich, sobald weniger als
    MIN_FREE des Speichers frei ist.
  * Zustand für die Oberfläche: STATUS (JSON).
"""
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from onvipilib import CONF, load_conf  # noqa: E402
from onvipi_camera import load_cameras  # noqa: E402

log = logging.getLogger("onvipi-record")

MOUNT = os.environ.get("ONVIPI_REC_MOUNT", "/media/onvipi-recordings")
LOCAL = "local"           # REC_UUID für die interne SD-Karte
LOCAL_DIR = os.environ.get("ONVIPI_REC_LOCAL", "/var/lib/onvipi/recordings")
LOCAL_MIN_FREE = 0.15     # die SD-Karte gehört zuerst dem Betriebssystem …
LOCAL_MIN_FREE_BYTES = 2 << 30   # … mindestens 2 GB bleiben frei
_base = MOUNT             # aktuelles Zielverzeichnis, siehe use()
STATUS = os.environ.get("ONVIPI_REC_STATUS", "/run/onvipi-record.json")
RUN_DIR = os.environ.get("ONVIPI_RUN_DIR", "/run/onvipi")
FOLDER = "onvipi"
MODES = ("off", "motion", "continuous")
FILESYSTEMS = ("ext4", "ext3", "ext2", "exfat", "vfat", "ntfs")
SEGMENT = {"continuous": 300, "motion": 60}
POST_ROLL = 15            # Sekunden nach dem Abschnitt, in denen Bewegung ihn noch rettet
MIN_FREE = 0.05           # Anteil, der frei bleiben soll …
MIN_FREE_BYTES = 500 << 20   # … mindestens aber 500 MB
FILE_RE = re.compile(r"(\d{8})-(\d{6})\.mp4")


def run(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as ex:
        return subprocess.CompletedProcess(cmd, 127, "", str(ex))


# --------------------------------------------------------------------------- Speicher
def use(uuid):
    """Legt fest, wo Aufnahmen liegen: USB-Speicher (MOUNT) oder SD-Karte (LOCAL_DIR)."""
    global _base
    _base = LOCAL_DIR if uuid == LOCAL else MOUNT
    return _base


def is_local():
    return _base == LOCAL_DIR


def local_storage():
    try:
        st = os.statvfs("/")
        size = st.f_blocks * st.f_frsize
    except (OSError, AttributeError):
        size = 0
    return {"uuid": LOCAL, "fstype": "", "label": "", "size": size,
            "model": "SD card (internal)", "local": True}


def storages():
    """Auswahl für die Oberfläche: SD-Karte + USB-Speicher mit nutzbarem Dateisystem."""
    out = run(["lsblk", "-J", "-b", "-o", "NAME,TRAN,TYPE,FSTYPE,LABEL,UUID,SIZE,MODEL"]).stdout
    try:
        disks = json.loads(out).get("blockdevices", [])
    except ValueError:
        disks = []
    found = [local_storage()]
    for disk in disks:
        if disk.get("tran") != "usb":
            continue
        for part in [disk] + (disk.get("children") or []):
            if part.get("uuid") and part.get("fstype") in FILESYSTEMS:
                found.append({"uuid": part["uuid"], "fstype": part["fstype"],
                              "label": part.get("label") or "", "size": int(part.get("size") or 0),
                              "model": (disk.get("model") or "USB storage").strip(),
                              "local": False})
    return found


def mounted_uuid():
    """UUID des unter MOUNT eingehängten Dateisystems (oder None)."""
    r = run(["findmnt", "-n", "-o", "UUID", "--mountpoint", MOUNT])
    return r.stdout.strip() or None if r.returncode == 0 else None


def mount(uuid):
    """Stellt das Ziel bereit (USB einhängen). Gibt eine Fehlermeldung oder None zurück."""
    use(uuid)
    if uuid == LOCAL:
        unmount()
        try:
            os.makedirs(LOCAL_DIR, mode=0o750, exist_ok=True)
        except OSError as ex:
            return f"The folder for recordings could not be created ({ex})."
        return None
    if mounted_uuid() == uuid:
        return None
    unmount()
    dev = next((s for s in storages() if s["uuid"] == uuid), None)
    if not dev:
        return "The storage device is not connected."
    os.makedirs(MOUNT, exist_ok=True)
    fstype = dev["fstype"]
    opts = "rw,noatime" + (",umask=022" if fstype in ("vfat", "exfat", "ntfs") else "")
    tries = [["-t", "ntfs3"], ["-t", "ntfs-3g"]] if fstype == "ntfs" else [["-t", fstype]]
    err = ""
    for kind in tries:
        r = run(["mount", *kind, "-o", opts, f"UUID={uuid}", MOUNT], timeout=60)
        if r.returncode == 0:
            log.info("Storage %s (%s) mounted", dev["label"] or uuid, fstype)
            return None
        err = r.stderr.strip()
    log.warning("Mounting %s failed: %s", uuid, err)
    return "The storage device could not be opened" + (f" ({err})." if err else ".")


def unmount():
    if os.path.ismount(MOUNT):
        run(["sync"], timeout=60)
        run(["umount", MOUNT], timeout=60)


def root():
    return _base if is_local() else os.path.join(MOUNT, FOLDER)


def camera_dir(cam_id):
    return os.path.join(root(), cam_id)


def start_of(name):
    """Startzeit (Epoch, Ortszeit des Pi) aus dem Dateinamen, None bei fremden Dateien."""
    m = FILE_RE.fullmatch(name)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"))
    except (ValueError, OverflowError):
        return None


def list_files(cam_id):
    """[(start, name, größe)] einer Kamera, älteste zuerst."""
    items = []
    try:
        with os.scandir(camera_dir(cam_id)) as it:
            for entry in it:
                start = start_of(entry.name)
                if start is not None:
                    try:
                        items.append((start, entry.name, entry.stat().st_size))
                    except OSError:
                        pass
    except OSError:
        pass
    return sorted(items)


def read_status():
    """Zustand des Dienstes für die Oberfläche; None, wenn er nicht (mehr) läuft."""
    try:
        with open(STATUS, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if time.time() - data.get("updated", 0) < 20 else None


# --------------------------------------------------------------------------- Aufnahme
class Recorder:
    def __init__(self):
        self.procs = {}       # Kamera-ID -> ffmpeg
        self.started = {}     # Kamera-ID -> letzter Startversuch
        self.motion = {}      # Kamera-ID -> [Zeitpunkte mit Bewegung]
        self.boot = time.time()
        self.last_prune = 0
        self.paused = False   # Notbremse: Speicher voll, obwohl nichts mehr zu löschen ist
        self.used = 0         # Bytes aller eigenen Aufnahmen (Stand des letzten Aufräumens)
        self.count = 0
        self.oldest = None    # Startzeit der ältesten Aufnahme

    def command(self, cam, conf, mode):
        url = (f"rtsp://{conf.get('CAM_USER', 'admin')}:{conf.get('CAM_PASSWORD', '')}"
               f"@127.0.0.1:8554/{cam['id']}")
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
                "-i", url, "-map", "0:v", "-map", "0:a?", "-c", "copy",
                "-f", "segment", "-segment_time", str(SEGMENT[mode]), "-segment_atclocktime", "1",
                "-reset_timestamps", "1", "-strftime", "1", "-segment_format", "mp4",
                "-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof",
                os.path.join(camera_dir(cam["id"]), "%Y%m%d-%H%M%S.mp4")]

    def ensure(self, cams, conf, mode):
        wanted = {c["id"]: c for c in cams}
        for cam_id in [i for i in self.procs if i not in wanted]:
            self.stop(cam_id)
        for cam_id, cam in wanted.items():
            proc = self.procs.get(cam_id)
            if proc and proc.poll() is None:
                continue
            if time.time() - self.started.get(cam_id, 0) < 10:
                continue  # Stream noch nicht bereit -> nicht im Sekundentakt neu starten
            if not is_local() and not os.path.ismount(MOUNT):
                return   # USB-Speicher fehlt: niemals ersatzweise auf die SD-Karte schreiben
            os.makedirs(camera_dir(cam_id), exist_ok=True)
            self.started[cam_id] = time.time()
            try:
                self.procs[cam_id] = subprocess.Popen(
                    self.command(cam, conf, mode), stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                log.info("[%s] recording started (%s)", cam_id, mode)
            except OSError as ex:
                log.error("[%s] could not start ffmpeg: %s", cam_id, ex)

    def stop(self, cam_id=None):
        for key in [cam_id] if cam_id else list(self.procs):
            proc = self.procs.pop(key, None)
            if proc and proc.poll() is None:
                proc.terminate()   # ffmpeg schließt den laufenden Abschnitt sauber ab
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()

    def note_motion(self, cams):
        now = time.time()
        for cam in cams:
            times = self.motion.setdefault(cam["id"], [])
            try:
                fresh = now - os.path.getmtime(os.path.join(RUN_DIR, f"motion-{cam['id']}")) < 4
            except OSError:
                fresh = False
            if fresh:
                times.append(now)
            while times and times[0] < now - 3600:
                times.pop(0)

    def drop_quiet(self, cams):
        """Modus "motion": abgeschlossene Abschnitte ohne Bewegung löschen."""
        now, seg = time.time(), SEGMENT["motion"]
        for cam in cams:
            if not cam["motion"]:
                continue   # ohne Bewegungserkennung alles behalten
            files = list_files(cam["id"])
            times = self.motion.get(cam["id"], [])
            for (start, name, _), nxt in zip(files, files[1:] + [None]):
                end = nxt[0] if nxt else start + seg
                if nxt is None or start < self.boot or now < end + POST_ROLL + 5:
                    continue   # läuft noch, stammt von vor dem Start, oder noch nicht entscheidbar
                if now - end > 1800:
                    continue   # längst entschieden
                if not any(start <= t <= end + POST_ROLL for t in times):
                    remove(os.path.join(camera_dir(cam["id"]), name))

    def prune(self, days, limit=0, force=False):
        """Ringspeicher. Löscht nacheinander: zu alte Aufnahmen, alles über der Obergrenze
        `limit` (Bytes, 0 = keine) und – solange die Reserve unterschritten ist – die ältesten.

        Der gerade geschriebene Abschnitt jeder Kamera bleibt immer stehen. `force` räumt
        sofort auf (freier Platz knapp), sonst höchstens alle 30 Sekunden.
        """
        now = time.time()
        if now - self.last_prune < (5 if force else 30):
            return
        self.last_prune = now
        try:
            cams = [d for d in os.listdir(root()) if os.path.isdir(os.path.join(root(), d))]
        except OSError:
            return
        per_cam = {cam: list_files(cam) for cam in cams}
        current = {(cam, files[-1][1]) for cam, files in per_cam.items() if files}
        files = sorted((start, cam, name, size) for cam, items in per_cam.items()
                       for start, name, size in items)
        used = sum(f[3] for f in files)
        kept = []

        def drop_oldest():
            """Löscht die älteste löschbare Aufnahme. False, wenn es keine mehr gibt."""
            nonlocal used
            while files:
                start, cam, name, size = files.pop(0)
                if (cam, name) in current:
                    kept.append((start, cam, name, size))
                    continue
                remove(os.path.join(camera_dir(cam), name))
                used -= size
                return True
            return False

        cutoff = now - days * 86400
        while files and files[0][0] < cutoff:
            if not drop_oldest():
                break
        while limit and used > limit:
            if not drop_oldest():
                break
        for _ in range(500):
            total, free = usage()
            if not total or free >= reserve(total) or not drop_oldest():
                break
        files = sorted(kept + files)
        self.used, self.count = used, len(files)
        self.oldest = files[0][0] if files else None

    def guard(self):
        """Notbremse: True, solange aufgenommen werden darf.

        Hält an, wenn nach dem Aufräumen weniger als die halbe Reserve frei ist, und gibt
        erst wieder frei, wenn die ganze Reserve frei ist (kein Flattern an der Grenze).
        """
        total, free = usage()
        if not total:
            return not self.paused
        need = reserve(total)
        if free < need / 2:
            if not self.paused:
                log.warning("Storage is full (%d MB free) – recording paused", free >> 20)
            self.paused = True
        elif free >= need and self.paused:
            log.info("Storage has space again – recording continues")
            self.paused = False
        return not self.paused


def thumb_path(path):
    """Vorschaubild einer Aufnahme (liegt in .thumbs neben den Videos)."""
    folder, name = os.path.split(path)
    return os.path.join(folder, ".thumbs", name[:-4] + ".jpg")


def thumbnail(path):
    """JPEG-Vorschaubild der Aufnahme, bei Bedarf erzeugt und zwischengespeichert (oder None)."""
    thumb = thumb_path(path)
    if not os.path.exists(thumb):
        try:
            os.makedirs(os.path.dirname(thumb), exist_ok=True)
        except OSError:
            return None
        r = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
                 "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "6", thumb], timeout=30)
        if r.returncode != 0:
            return None
    try:
        with open(thumb, "rb") as f:
            return f.read()
    except OSError:
        return None


def remove(path):
    for target in (path, thumb_path(path)):
        try:
            os.remove(target)
        except OSError:
            pass


def reserve(total):
    """So viel muss frei bleiben, sonst werden die ältesten Aufnahmen gelöscht."""
    if is_local():
        return max(total * LOCAL_MIN_FREE, LOCAL_MIN_FREE_BYTES)
    return max(total * MIN_FREE, MIN_FREE_BYTES)


def usage():
    try:
        st = os.statvfs(_base)
        return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
    except (OSError, AttributeError):
        return 0, 0


def write_status(**data):
    data["updated"] = time.time()
    tmp = STATUS + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, STATUS)
    except OSError:
        pass


def settings(conf):
    mode = conf.get("REC_MODE", "off")
    try:
        days = max(1, min(365, int(conf.get("REC_DAYS", "14"))))
    except ValueError:
        days = 14
    return (mode if mode in MODES else "off"), conf.get("REC_UUID", ""), days


def max_bytes(conf):
    """Obergrenze für alle Aufnahmen zusammen in Bytes (0 = keine)."""
    try:
        gb = float(str(conf.get("REC_MAX_GB", "") or 0).replace(",", "."))
    except ValueError:
        return 0
    return int(gb * 1e9) if gb > 0 else 0


def main():
    logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"), format="%(name)s: %(message)s")
    rec = Recorder()

    def stop(*_):
        rec.stop()
        unmount()
        try:
            os.remove(STATUS)
        except OSError:
            pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    last_error = None
    while True:
        conf = load_conf(CONF)
        mode, uuid, days = settings(conf)
        if mode == "off" or not uuid:
            rec.stop()
            unmount()
            write_status(mode="off", mounted=False, error="", recording=[])
            time.sleep(5)
            continue
        error = mount(uuid)
        if error:
            rec.stop()
            if error != last_error:
                log.warning("Not recording: %s", error)
            last_error = error
            write_status(mode=mode, mounted=False, error=error, recording=[])
            time.sleep(15)
            continue
        last_error = None
        cams = [c for c in load_cameras(conf) if c["enabled"]]
        limit = max_bytes(conf)
        total, free = usage()
        # erst Platz schaffen, dann schreiben; bei knappem Platz sofort statt im 30-s-Takt
        rec.prune(days, limit, force=bool(total) and free < reserve(total))
        error = ""
        if rec.guard():
            rec.ensure(cams, conf, mode)
            if mode == "motion":
                rec.note_motion(cams)
                rec.drop_quiet(cams)
        else:
            rec.stop()
            error = ("The storage is full with other data. Recording is paused and continues "
                     "as soon as there is free space again.")
        total, free = usage()
        write_status(mode=mode, mounted=True, error=error, total=total, free=free,
                     local=is_local(), reserve=reserve(total), limit=limit,
                     used=rec.used, count=rec.count, oldest=rec.oldest, days=days,
                     recording=[i for i, p in rec.procs.items() if p.poll() is None])
        time.sleep(2)


if __name__ == "__main__":
    main()
