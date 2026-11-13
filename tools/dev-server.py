#!/usr/bin/env python3
"""Runs the OnviPi web interface on a development machine – no Raspberry Pi needed.

    python tools/dev-server.py             settings interface (password: onvipi-dev)
    python tools/dev-server.py --setup     setup wizard (fresh, unconfigured device)
    python tools/dev-server.py --cameras 1 --no-ethernet --port 8080

Everything that would touch the system (NetworkManager, users, services, webcams) is
replaced by a small simulation; the real server code, API and pages are used unchanged.
In the wizard, the Wi-Fi password "wrongpass" simulates a failed connection.
"""
import argparse
import base64
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASSWORD = "onvipi-dev"

ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
ap.add_argument("--setup", action="store_true", help="start unconfigured (setup wizard)")
ap.add_argument("--cameras", type=int, default=2, help="number of simulated webcams")
ap.add_argument("--no-ethernet", action="store_true", help="simulate a device without cable")
ap.add_argument("--usb-wifi", action="store_true", help="simulate an additional USB Wi-Fi adapter")
ap.add_argument("--port", type=int, default=8080)
args = ap.parse_args()

conf_dir = tempfile.mkdtemp(prefix="onvipi-dev-")
os.environ["ONVIPI_CONF_DIR"] = conf_dir
os.environ["ONVIPI_PORTAL_PORT"] = str(args.port)
sys.path.insert(0, os.path.join(ROOT, "onvipi", "pi"))

import onvipi_camera as camera  # noqa: E402
import onvipi_net as netlib  # noqa: E402
import onvipi_record as record  # noqa: E402
import onvipi_system as system  # noqa: E402
import onvipi_update as update  # noqa: E402
import onvipi_web as web  # noqa: E402
from onvipilib import CONF, load_conf, save_conf  # noqa: E402

sim = {"configured": not args.setup, "ssh": False, "timezone": "Europe/Berlin",
       "wifi": None if args.setup else "wifi-Home", "saved": [] if args.setup else ["wifi-Home"],
       "ip": {"method": "auto", "address": "", "prefix": "24", "gateway": "", "dns": []}}

save_conf(CONF, {"CAM_NAME": "Garden", "CAM_USER": "admin", "CAM_PASSWORD": PASSWORD,
                 "AP_SSID": "OnviPi-Setup-1A2B", "WIFI_IFACE": "wlan0", "WIFI_COUNTRY": "DE",
                 "ONVIF_PORT": "8000", "HOSTNAME": "onvipi-garden", "HOSTNAME_AUTO": "1"})

# ---------------------------------------------------------------- cameras
FORMATS = {"mjpeg": {"1920x1080": [30, 15], "1280x720": [30, 15, 10], "640x480": [30]},
           "yuyv422": {"640x480": [30, 15]}}
DEVICES = [{"dev": f"/dev/v4l/by-id/usb-Demo_Webcam_{n}-video-index0",
            "label": ["Logitech C920", "Generic HD Webcam", "USB 2.0 Camera", "ELP Night Vision"][n % 4],
            "formats": FORMATS, "mic": n % 2 == 0} for n in range(args.cameras)]
camera.list_devices = lambda: DEVICES
camera.camera_label = lambda dev: next((d["label"] for d in DEVICES if d["dev"] == dev), dev)
camera.same_device = lambda a, b: bool(a and b) and a == b
if not args.setup or DEVICES:
    cams = []
    for dev in (DEVICES if args.setup else DEVICES[:1]):
        cams.append(camera.new_camera(cams, dev, load_conf(CONF)))
    camera.pin_mac(cams, fresh=True)
    camera.save_cameras(cams)

try:   # a real JPEG looks nicer; without Pillow a tiny grey one is used
    from io import BytesIO
    from PIL import Image, ImageDraw

    def snapshot(cam_id):
        img = Image.new("RGB", (1280, 720), (28, 44, 62))
        draw = ImageDraw.Draw(img)
        for i in range(0, 1280, 80):
            draw.line([(i, 0), (i + 360, 720)], fill=(38, 60, 84), width=3)
        draw.text((40, 40), f"Demo picture · {cam_id}", fill=(230, 236, 242))
        draw.text((40, 70), time.strftime("%d.%m.%Y %H:%M:%S"), fill=(230, 236, 242))
        buf = BytesIO()
        img.save(buf, "JPEG", quality=70)
        return buf.getvalue()
except ImportError:
    GREY = base64.b64decode(
        "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////"
        "////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAB"
        "PxA=")

    def snapshot(cam_id):
        return GREY
web.App.snapshot = staticmethod(snapshot)

# ---------------------------------------------------------------- live view (stands in for go2rtc)
import http.server  # noqa: E402

PLAYER = b"""<!doctype html><meta charset=utf-8><style>
html,body{margin:0;height:100%;background:#1c2c3e;overflow:hidden;font:600 4vw system-ui;color:#e6ecf2}
i{position:absolute;inset:-50%;background:repeating-linear-gradient(115deg,#1c2c3e 0 40px,#263c54 40px 44px);
animation:m 6s linear infinite}@keyframes m{to{transform:translateX(88px)}}
p{position:absolute;left:4%;bottom:5%;margin:0}</style><i></i><p id=t></p><script>
setInterval(function(){t.textContent="Demo live view - "+new Date().toLocaleTimeString()},500)</script>"""


class FakePlayer(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.path.startswith("/stream.html")
        self.send_response(200 if ok else 404)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PLAYER) if ok else 0))
        self.end_headers()
        if ok and self.command == "GET":
            self.wfile.write(PLAYER)
    do_HEAD = do_GET

    def log_message(self, *a):
        pass


web.GO2RTC_PORT = args.port + 1000
threading.Thread(target=http.server.ThreadingHTTPServer(("127.0.0.1", web.GO2RTC_PORT), FakePlayer).serve_forever,
                 daemon=True).start()

# ---------------------------------------------------------------- recordings
rec_dir = os.path.join(conf_dir, "recordings")
record.LOCAL_DIR = rec_dir
record.use(record.LOCAL)
record.storages = lambda: [
    {"uuid": "local", "fstype": "", "label": "", "size": 32 << 30, "model": "SD card (internal)", "local": True},
    {"uuid": "1234-ABCD", "fstype": "exfat", "label": "CAMSTICK", "size": 64 << 30, "model": "SanDisk Ultra", "local": False}]
record.mounted_uuid = lambda: load_conf(CONF).get("REC_UUID") or None
record.use = lambda uuid: rec_dir
record.read_status = lambda: {"mode": load_conf(CONF).get("REC_MODE", "off"), "mounted": True, "error": "",
                              "total": 64 << 30, "free": 41 << 30, "updated": time.time(),
                              "reserve": 3 << 30, "limit": record.max_bytes(load_conf(CONF)), "days": 14,
                              "used": 19 << 30, "count": 5184, "oldest": time.time() - 6 * 86400,
                              "recording": [c["id"] for c in camera.load_cameras() if c["enabled"]]}
record.thumbnail = lambda path: snapshot(os.path.basename(path)[9:15])
if not args.setup:
    conf = load_conf(CONF)
    conf.update(REC_MODE="motion", REC_UUID="1234-ABCD", REC_DAYS="14")
    save_conf(CONF, conf)
    now = time.time()
    for cam_id in ("onvipi", "cam2"):
        os.makedirs(os.path.join(rec_dir, cam_id), exist_ok=True)
        for n in range(46 if cam_id == "onvipi" else 9):
            t = now - n * 1900 - (200 if cam_id == "cam2" else 0)
            with open(os.path.join(rec_dir, cam_id, time.strftime("%Y%m%d-%H%M%S.mp4", time.localtime(t))), "wb") as f:
                f.write(b"\0" * (3_000_000 + n * 40_000))
for cam_id in (() if args.setup else ("onvipi", "cam2")):   # movements that explain the clips
    with open(os.path.join(rec_dir, cam_id, record.EVENTS), "w") as f:
        for start, _, _ in record.list_files(cam_id)[::2]:
            f.write(f"{start + 12:.1f} {start + 31:.1f} 14.2\n{start + 140:.1f} {start + 149:.1f} 9.8\n")
web.motion_state = lambda cam_id: {
    "subs": 1, "analysing": True, "motion": int(time.time()) % 20 < 6, "since": 0,
    "last": [time.time() - 340, time.time() - 322], "threshold": 8.9,
    "score": 12.5 if int(time.time()) % 20 < 6 else 1.2 + (int(time.time() * 2) % 5) * 0.4, "updated": time.time()}
update.check = lambda: {"current": "dev", "latest": "2.2", "available": True,
                        "url": "https://github.com/GERGto/OnviPi/releases"}
update.start = lambda: update.write_status("done", version="2.2")
update.STATUS = os.path.join(conf_dir, "update.json")
update.STATE = os.path.join(conf_dir, "update-state.json")
update.save_state(last_check=time.time() - 9 * 3600, latest="2.2", available=False, check_error="",
                  last_update={"version": "2.2", "time": time.time() - 4 * 86400, "ok": True, "error": ""})
system.factory_reset = lambda: print("[sim] factory reset")
system.NM_DIR = os.path.join(conf_dir, "nm")

# ---------------------------------------------------------------- network
WIFI = [{"iface": "wlan0", "usb": False, "label": "Built-in Wi-Fi"}]
if args.usb_wifi:
    WIFI.append({"iface": "wlan1", "usb": True, "label": "USB Wi-Fi adapter (Realtek 802.11ac NIC)"})
NETWORKS = [("Home", 82, "WPA2"), ("Home-Guest", 74, "--"), ("Neighbour 5G", 41, "WPA2 WPA3"),
            ("FRITZ!Box 7590 XY", 33, "WPA2")]


def devices():
    items = []
    if not args.no_ethernet:
        items.append({"iface": "eth0", "type": "ethernet", "state": "connected",
                      "connection": "Wired connection 1"})
    for w in WIFI:
        mine = w["iface"] == "wlan0"
        hotspot = mine and not sim["configured"]
        items.append({"iface": w["iface"], "type": "wifi",
                      "state": "connected" if mine and (sim["wifi"] or hotspot) else "disconnected",
                      "connection": netlib.HOTSPOT if hotspot else (sim["wifi"] or "") if mine else ""})
    return items


def interfaces():
    out = []
    for d in devices():
        up = d["state"] == "connected" and d["connection"] != netlib.HOTSPOT
        wifi = d["type"] == "wifi"
        out.append(dict(d, usb=d["iface"] == "wlan1", mac="dc:a6:32:00:00:0" + d["iface"][-1],
                        label=next((w["label"] for w in WIFI if w["iface"] == d["iface"]),
                                   "Ethernet (cable)"),
                        state="hotspot" if d["connection"] == netlib.HOTSPOT else d["state"],
                        ip=("192.168.1.57" if wifi else "192.168.1.56") if up else "",
                        prefix="24" if up else "", gateway="192.168.1.1" if up else "",
                        dns=["192.168.1.1"] if up else [], method=sim["ip"]["method"] if up else "",
                        ssid="Home" if up and wifi else "", signal=78 if up and wifi else None))
    return out


def apply_ip(connection, cfg):
    sim["ip"] = cfg
    return None


netlib.devices = devices
netlib.interfaces = interfaces
netlib.wifi_devices = lambda: list(WIFI)
netlib.ethernet_connected = lambda: not args.no_ethernet
netlib.ip_config = lambda connection: dict(sim["ip"])
netlib.apply_ip_config = apply_ip
netlib.Net.scan = lambda self, iface=None: (time.sleep(1.5), list(NETWORKS))[1]
netlib.Net.saved_wifi = lambda self: list(sim["saved"])
netlib.Net.forget = lambda self, name: name in sim["saved"] and not sim["saved"].remove(name)
web.primary_ip = lambda: "192.168.1.56" if not args.no_ethernet else (
    "192.168.1.57" if sim["wifi"] else None)

# ---------------------------------------------------------------- system
system.configured = lambda: sim["configured"]
system.timezones = lambda: ["Europe/Berlin", "Europe/London", "Europe/Vienna", "Europe/Zurich",
                            "America/New_York", "UTC"]
system.current_timezone = lambda: sim["timezone"]
system.set_timezone = lambda tz: sim.update(timezone=tz) or True
system.set_country = lambda code: None
system.set_hostname = lambda new: None
system.ssh_enabled = lambda: sim["ssh"]
system.ssh_fingerprints = lambda: ["ED25519 SHA256:kB/jcXYpmeFooFzB/DwQjrlSIObRx1DcmyW9XEoulpM"]
system.set_ssh = lambda enable: sim.update(ssh=bool(enable)) or True
system.users_with_password = lambda: ["admin"] if sim["configured"] else []
system.set_login_password = lambda name, password: True
system.reboot = lambda: print("[sim] reboot")
system.logs = lambda lines=800: "2026-11-11T10:00:00+0100 onvipi-go2rtc: demo log line\n" * 20
system.version = lambda: "dev"
system.default_username = lambda conf: conf.get("LOGIN_USER") or "admin"
system.stats = lambda: {"cpu_temp": 52.3, "cpu_percent": 34 + int(time.time()) % 9, "cores": 4,
                        "mem_percent": 41, "disk_percent": 23, "uptime": 93784,
                        "undervoltage": False, "undervoltage_seen": False, "throttled": False}
web.restart_services = lambda *units: print("[sim] restart", *(units or ["all services"]))
web.App.hostname = staticmethod(lambda: load_conf(CONF).get("HOSTNAME", "onvipi"))


def worker(app):
    """Stands in for the service loop of onvipi_wifi.py."""
    while True:
        job = app.take_setup_job()
        if job:
            time.sleep(4)
            if job["wifi"] and job["wifi"]["psk"] == "wrongpass":
                app.setup_progress = {"phase": "failed", "error": (
                    f"Could not connect to “{job['wifi']['ssid']}”. Please check the Wi-Fi "
                    "password and try again.")}
            else:
                conf = load_conf(CONF)
                conf.update(CAM_USER=job["user"], CAM_PASSWORD=job["password"],
                            LOGIN_USER=job["user"])
                save_conf(CONF, conf)
                if job["wifi"]:
                    sim["wifi"] = "wifi-" + job["wifi"]["ssid"]
                    sim["saved"].append(sim["wifi"])
                sim["configured"] = True
                app.setup_progress = {"phase": "done", "error": "", "ip": web.primary_ip()}
        job = app.take_wifi_job()
        if job:
            time.sleep(4)
            if job[1] == "wrongpass":
                app.wifi_status = {"phase": "failed", "ssid": job[0], "error": (
                    f"Could not connect to “{job[0]}”. Please check the Wi-Fi password.")}
            else:
                sim["wifi"] = "wifi-" + job[0]
                if sim["wifi"] not in sim["saved"]:
                    sim["saved"].append(sim["wifi"])
                app.wifi_status = {"phase": "done", "ssid": job[0], "error": ""}
        app.wake.wait(1)
        app.wake.clear()


net = netlib.Net(load_conf(CONF))
app = web.App(net)
app.networks = list(NETWORKS)
app.ap = args.setup
web.App.setup_state = (lambda orig: lambda self, via_hotspot: orig(self, args.setup and args.no_ethernet))(
    web.App.setup_state)
app.start()
threading.Thread(target=worker, args=(app,), daemon=True).start()
print(f"OnviPi dev server: http://localhost:{args.port}/   "
      f"({'setup wizard' if args.setup else 'password: ' + PASSWORD})   config: {conf_dir}")
try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    app.stop()
