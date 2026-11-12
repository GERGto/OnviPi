#!/usr/bin/env python3
"""Weboberfläche von OnviPi: Einrichtungsassistent, Einstellungen und JSON-Schnittstelle.

Die Oberfläche selbst liegt als statische Dateien in web/ (kein Build-Schritt):

  setup.html + setup.js   Assistent – solange noch kein Konto eingerichtet ist
  index.html + app.js     Einstellungen – mit Anmeldung (Sitzungs-Cookie)

Zugriffsregeln:
  * Nicht eingerichtet: Der Assistent ist offen (Hotspot und LAN), sonst nichts.
  * Eingerichtet: alles außer Anmeldung und Statusabfrage braucht eine Sitzung.
    Das gilt auch am Einrichtungs-Hotspot (WLAN-Ausfall) – dort öffnet sich die
    Anmeldeseite, kein offener Assistent.
  * Schreibende Aufrufe nur von der eigenen Seite (Origin-Prüfung, SameSite-Cookie).

Der Dienst onvipi_wifi startet den Server und arbeitet die Aufträge ab, die das Netz
umschalten (WLAN verbinden, Einrichtung abschließen) – siehe App.take_*().
"""
import hmac
import http.server
import json
import logging
import os
import re
import secrets
import select
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request

import onvipi_camera as camera
import onvipi_net as netlib
import onvipi_record as record
import onvipi_system as system
import onvipi_update as update
from onvipilib import CONF, MQTT_CONF, STREAM_NAME, load_conf, primary_ip, save_conf

log = logging.getLogger("onvipi-web")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PORT = int(os.environ.get("ONVIPI_PORTAL_PORT", "80"))
LOGIN_MAX_FAILS = 5          # danach ist die Adresse LOGIN_LOCK Sekunden gesperrt
LOGIN_LOCK = 600
SESSION_TTL = 24 * 3600      # verlängert sich bei Benutzung
COOKIE = "onvipi_sid"
SERVICES = ["onvipi-go2rtc.service", "onvipi-onvif.service", "onvipi-wsd.service",
            "onvipi-mqtt.service"]
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
        ".ico": "image/x-icon", ".json": "application/json"}
HOSTNAME_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


class ApiError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message, self.code = message, code


def clean_name(text):
    return re.sub(r"[\x00-\x1f'\"\\`$<>&]", "", str(text or ""))[:48].strip()


def restart_services(*units):
    """Dienste im Hintergrund neu starten – die HTTP-Antwort soll nicht darauf warten."""
    subprocess.Popen(["systemctl", "restart", *(units or SERVICES)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class App:
    def __init__(self, net):
        self.net = net
        self.lock = threading.Lock()
        self.wake = threading.Event()      # weckt die Dienstschleife (neuer Auftrag)
        self.sessions = {}                 # Token -> Ablaufzeit
        self.fails = {}                    # IP -> (Fehlversuche, Zeitpunkt des letzten)
        self.ip_change = netlib.IpChange()
        self.terminal = system.Terminal()
        self.ap = False                    # Einrichtungs-Hotspot aktiv
        self.networks = []                 # letzter WLAN-Scan [(ssid, signal, security)]
        self.wifi_job = None               # (ssid, psk, hidden)
        self.wifi_status = {"phase": "idle", "ssid": "", "error": ""}
        self.setup_job = None
        self.setup_draft = {}              # Eingaben des Assistenten (für einen 2. Versuch)
        self.setup_progress = {"phase": "idle", "error": ""}
        self.server = None
        self.thumbs = threading.Semaphore(2)   # höchstens zwei Vorschaubilder gleichzeitig

    # -- Sitzungen
    def locked(self, ip):
        n, since = self.fails.get(ip, (0, 0))
        if n >= LOGIN_MAX_FAILS and time.time() - since < LOGIN_LOCK:
            return True
        if n and time.time() - since >= LOGIN_LOCK:
            self.fails.pop(ip, None)
        return False

    def login(self, ip, password):
        if self.locked(ip):
            raise ApiError("Too many failed attempts – please wait 10 minutes.", 429)
        expected = load_conf(CONF).get("CAM_PASSWORD", "")
        if expected and hmac.compare_digest(str(password).encode(), expected.encode()):
            self.fails.pop(ip, None)
            token = secrets.token_urlsafe(32)
            with self.lock:
                now = time.time()
                self.sessions = {t: exp for t, exp in self.sessions.items() if exp > now}
                self.sessions[token] = now + SESSION_TTL
            return token
        n = self.fails.get(ip, (0, 0))[0] + 1
        self.fails[ip] = (n, time.time())
        log.warning("Failed login from %s (%d)", ip, n)
        left = LOGIN_MAX_FAILS - n
        raise ApiError("Wrong password." + (f" {left} attempts left." if 0 < left < 3 else ""),
                       401)

    def session_valid(self, token):
        with self.lock:
            exp = self.sessions.get(token or "")
            if not exp or exp < time.time():
                self.sessions.pop(token, None)
                return False
            self.sessions[token] = time.time() + SESSION_TTL
            return True

    def logout(self, token, others=False):
        with self.lock:
            if others:
                self.sessions = {t: e for t, e in self.sessions.items() if t == token}
            else:
                self.sessions.pop(token, None)

    # -- Aufträge für die Dienstschleife
    def take_wifi_job(self):
        with self.lock:
            job, self.wifi_job = self.wifi_job, None
            return job

    def take_setup_job(self):
        with self.lock:
            job, self.setup_job = self.setup_job, None
            return job

    # -- Allgemein
    @staticmethod
    def hostname():
        return socket.gethostname() or load_conf(CONF).get("HOSTNAME", "onvipi")

    def networks_json(self):
        return [{"ssid": s, "signal": sig, "secure": bool(sec and sec != "--")}
                for s, sig, sec in self.networks]

    def scan(self, iface=None):
        """Scannt, wenn das Gerät gerade nicht den Hotspot trägt; sonst der letzte Stand."""
        iface = iface or self.net.iface
        if self.ap and iface == self.net.ap_iface:
            return False
        self.networks = self.net.scan(iface)
        return True

    @staticmethod
    def snapshot(cam_id):
        if not re.fullmatch(r"[a-z0-9]+", cam_id or ""):
            return None
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:1984/api/frame.jpeg?src={cam_id}", timeout=15) as r:
                return r.read()
        except OSError as ex:
            log.debug("Snapshot failed: %s", ex)
            return None

    # -- Kameras
    def save_cameras(self, items, wizard=False):
        if not isinstance(items, list) or not items:
            raise ApiError("No camera settings received.")
        conf = load_conf(CONF)
        cams = camera.load_cameras(conf)
        devices = camera.list_devices()
        for item in items:
            cam = camera.get_camera(cams, str(item.get("id") or ""))
            if cam is None:   # neu erkanntes Gerät
                dev = next((d for d in devices if d["dev"] == item.get("device")), None)
                if not dev or any(camera._same(c["device"], dev["dev"]) for c in cams):
                    raise ApiError("This camera is no longer connected – please reload.")
                cam = camera.new_camera(cams, dev, conf)
                cams.append(cam)
            dev = next((d for d in devices if camera._same(cam["device"], d["dev"])), None)
            if "name" in item:
                cam["name"] = clean_name(item["name"]) or cam["name"]
            for key in ("enabled", "overlay", "motion", "audio", "substream"):
                if key in item:
                    cam[key] = bool(item[key])
            for key in ("motion_zones", "privacy"):   # normalize() prüft die Rechtecke
                if key in item:
                    cam[key] = item[key]
            if "sensitivity" in item:
                cam["sensitivity"] = item["sensitivity"]
            if "rotate" in item:
                cam["rotate"] = item["rotate"]
            if dev and item.get("format"):
                fmt, size = str(item["format"]), str(item.get("size") or "")
                if size not in dev["formats"].get(fmt, {}):
                    raise ApiError(f"“{cam['name']}”: this resolution is not supported.")
                allowed = dev["formats"][fmt][size]
                try:
                    fps = int(item.get("fps"))
                except (TypeError, ValueError):
                    fps = allowed[0] if allowed else cam["fps"]
                if allowed and fps not in allowed:
                    fps = allowed[0]
                cam.update(format=fmt, size=size, fps=fps)
            cam.update(camera.normalize(cam))
        if sum(c["enabled"] for c in cams) > camera.MAX_CAMERAS:
            raise ApiError(f"At most {camera.MAX_CAMERAS} cameras can be active at once.")
        camera.pin_mac(cams)
        camera.save_cameras(cams)

        first = camera.get_camera(cams, STREAM_NAME)
        if first and first["name"] != conf.get("CAM_NAME"):
            conf["CAM_NAME"] = first["name"]
            if wizard and conf.get("HOSTNAME_AUTO") == "1":
                # Hostname folgt dem Namen, solange er automatisch vergeben wurde
                new_host = system.hostname_for(first["name"])
                if new_host:
                    system.set_hostname(new_host)
                    conf["HOSTNAME"] = new_host
            save_conf(CONF, conf)
        log.info("Cameras saved: %s", ", ".join(
            f"{c['name']} ({'on' if c['enabled'] else 'off'})" for c in cams))
        restart_services()

    def remove_camera(self, cam_id):
        cams = camera.load_cameras()
        if not camera.get_camera(cams, cam_id):
            raise ApiError("Unknown camera.")
        camera.save_cameras([c for c in cams if c["id"] != cam_id])
        restart_services()

    # -- Einrichtung
    def setup_state(self, via_hotspot):
        conf = load_conf(CONF)
        ifaces = netlib.devices()
        eth = any(d["type"] == "ethernet" and d["state"] == "connected" for d in ifaces)
        draft = {k: v for k, v in self.setup_draft.items() if k != "password"}
        draft["has_password"] = bool(self.setup_draft.get("password"))
        return {
            "name": conf.get("CAM_NAME", "OnviPi"),
            "hostname": self.hostname(),
            "cameras": camera.overview(),
            "max_cameras": camera.MAX_CAMERAS,
            "username": system.default_username(conf),
            "password_hint": system.PASSWORD_HINT,
            "timezone": system.current_timezone(),
            "timezones": system.timezones(),
            "country": conf.get("WIFI_COUNTRY", "DE"),
            "countries": system.countries(),
            "network": {
                "ethernet": eth, "ip": primary_ip() if eth else None,
                "via_hotspot": via_hotspot, "ap_ssid": self.net.ap_ssid,
                "wifi_devices": self.net.refresh(), "wifi_iface": self.net.iface,
                "stays_connected": eth or self.net.independent,
                "networks": self.networks_json(),
            },
            "draft": draft,
            "progress": self.setup_progress,
        }

    def setup_finish(self, data):
        if system.configured():
            raise ApiError("This device is already set up.", 403)
        conf = load_conf(CONF)
        user = str(data.get("user") or system.default_username(conf)).strip().lower()
        password = str(data.get("password") or self.setup_draft.get("password") or "")
        err = system.check_account(user, password)
        if err:
            raise ApiError(err)
        tz = str(data.get("timezone") or system.current_timezone())
        country = str(data.get("country") or conf.get("WIFI_COUNTRY", "DE")).upper()
        if tz not in system.timezones():
            raise ApiError("Please select a time zone.")
        if country not in {c for c, _ in system.countries()}:
            raise ApiError("Please select a Wi-Fi country.")
        wifi = data.get("wifi") or None
        if wifi:
            wifi = self.check_wifi(wifi)
        elif not netlib.ethernet_connected():
            raise ApiError("Please choose a Wi-Fi network or plug in a network cable.")
        job = {"user": user, "password": password, "timezone": tz, "country": country,
               "wifi": wifi}
        with self.lock:
            self.setup_draft = dict(job, wifi={"ssid": wifi["ssid"], "iface": wifi["iface"]}
                                    if wifi else None)
            self.setup_job = job
            self.setup_progress = {"phase": "working", "error": ""}
        self.wake.set()

    def check_wifi(self, wifi):
        ssid = str(wifi.get("ssid") or "").strip()
        psk = str(wifi.get("psk") or "")
        if not ssid or len(ssid.encode()) > 32:
            raise ApiError("Please select or enter a Wi-Fi network.")
        if psk and not 8 <= len(psk) <= 63:
            raise ApiError("The Wi-Fi password must be 8 to 63 characters long.")
        names = [d["iface"] for d in self.net.refresh()]
        iface = str(wifi.get("iface") or self.net.iface)
        if iface not in names:
            raise ApiError("The selected Wi-Fi adapter is not available.")
        hidden = bool(wifi.get("hidden")) or ssid not in {n[0] for n in self.networks}
        return {"ssid": ssid, "psk": psk, "hidden": hidden, "iface": iface}

    def select_wifi_iface(self, iface):
        if iface and iface != self.net.wanted:
            conf = load_conf(CONF)
            conf["WIFI_IFACE"] = iface
            save_conf(CONF, conf)
            self.net.select(iface)
            log.info("Wi-Fi adapter for the home network: %s", iface)

    # -- Passwort
    def change_password(self, token, current, new):
        conf = load_conf(CONF)
        if not hmac.compare_digest(str(current).encode(), conf.get("CAM_PASSWORD", "").encode()):
            raise ApiError("The current password is not correct.", 403)
        if not system.PASSWORD_RE.fullmatch(str(new)):
            raise ApiError("Password: " + system.PASSWORD_HINT)
        conf["CAM_PASSWORD"] = new
        save_conf(CONF, conf)
        if conf.get("LOGIN_USER"):
            system.set_login_password(conf["LOGIN_USER"], new)
        self.logout(token, others=True)
        log.info("Password changed via web interface")
        restart_services("onvipi-go2rtc.service", "onvipi-onvif.service")

    # -- Server
    def start(self):
        http.server.ThreadingHTTPServer.allow_reuse_address = True
        http.server.ThreadingHTTPServer.daemon_threads = True
        self.server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(self))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        log.info("Web interface running on port %d", PORT)

    def stop(self):
        self.terminal.stop()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


# --------------------------------------------------------------------------- HTTP
def make_handler(app):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "OnviPi"

        def log_message(self, fmt, *args):
            log.debug("http: " + fmt, *args)

        # -- Antworten
        def send(self, code, body=b"", ctype=None, headers=None, cache=False):
            self.send_response(code)
            if ctype:
                self.send_header("Content-Type", ctype)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)

        def json(self, data=None, code=200, headers=None):
            body = json.dumps({"ok": True} if data is None else data).encode()
            self.send(code, body, "application/json", headers)

        def redirect(self, location):
            self.send(302, headers={"Location": location})

        def static(self, name):
            path = os.path.join(WEB_DIR, name)
            if name not in os.listdir(WEB_DIR) or not os.path.isfile(path):
                return self.send(404, b"Not found\n", "text/plain")
            with open(path, "rb") as f:
                body = f.read()
            ext = os.path.splitext(name)[1]
            headers = {}
            version = system.version()
            if ext == ".html":
                headers = {"X-Frame-Options": "SAMEORIGIN", "Referrer-Policy": "same-origin"}
                # Skripte/Styles pro Version neu laden lassen (sonst alter Stand im Browser-Cache)
                body = body.replace(b"?v=3", f"?v={urllib.parse.quote(version)}".encode())
            self.send(200, body, MIME.get(ext, "application/octet-stream"), headers,
                      cache=ext != ".html" and version != "dev" and "v=" in self.path)

        # -- Anfrage
        def token(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE:
                    return v
            return ""

        def authenticated(self):
            return app.session_valid(self.token())

        def body(self):
            length = min(int(self.headers.get("Content-Length") or 0), 262144)
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                raise ApiError("Invalid request.")
            if not isinstance(data, dict):
                raise ApiError("Invalid request.")
            return data

        def same_origin(self):
            """Schreibende Aufrufe nur von dieser Seite (Schutz vor fremden Webseiten)."""
            src = self.headers.get("Origin") or self.headers.get("Referer") or ""
            return urllib.parse.urlparse(src).netloc == (self.headers.get("Host") or "")

        def via_hotspot(self):
            return app.ap and self.client_address[0].startswith(netlib.AP_NET)

        def captive(self):
            """Hotspot-Clients mit fremder Adresse (Captive-Portal-Checks) -> zur Oberfläche."""
            host = (self.headers.get("Host") or "").split(":")[0].lower()
            return self.via_hotspot() and not (
                host in (netlib.AP_IP, "localhost", "127.0.0.1") or host.endswith(".local"))

        # -- Verteilung
        def do_GET(self):
            self.dispatch("GET")

        def do_HEAD(self):
            self.dispatch("GET")

        def do_POST(self):
            self.dispatch("POST")

        def dispatch(self, method):
            parts = urllib.parse.urlparse(self.path)
            path, query = parts.path, urllib.parse.parse_qs(parts.query)
            try:
                if self.captive():
                    return self.redirect(f"http://{netlib.AP_IP}/")
                if path.startswith("/api/"):
                    if method == "POST" and not self.same_origin():
                        raise ApiError("Forbidden.", 403)
                    return self.api(method, path[5:], query)
                if path == system.Terminal.BASE or path.startswith(system.Terminal.BASE + "/"):
                    return self.terminal_proxy()
                if method != "GET":
                    return self.send(405)
                if path.startswith("/static/"):
                    return self.static(path[8:])
                if path.startswith("/recordings/"):
                    return self.recording(path[12:], "download" in query)
                if path in ("/", "/index.html"):
                    return self.static("index.html" if system.configured() else "setup.html")
                self.redirect("/")
            except ApiError as ex:
                self.json({"ok": False, "error": ex.message}, ex.code)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                log.exception("Error handling %s %s", method, path)
                self.json({"ok": False, "error": "Internal error – see the log."}, 500)

        def api(self, method, route, query):
            configured = system.configured()
            # ---- immer offen
            if route == "state":
                conf = load_conf(CONF)
                return self.json({
                    "configured": configured,
                    "authenticated": configured and self.authenticated(),
                    "name": conf.get("CAM_NAME", "OnviPi"), "user": conf.get("CAM_USER", "admin"),
                    "hostname": app.hostname(), "version": system.version(),
                    "hotspot": self.via_hotspot()})
            if route == "setup/progress":
                return self.json(dict(app.setup_progress, hostname=app.hostname(),
                                      configured=configured))
            if route == "login" and method == "POST":
                if not configured:
                    raise ApiError("This device is not set up yet.", 403)
                token = app.login(self.client_address[0], self.body().get("password", ""))
                return self.json(headers={"Set-Cookie": (
                    f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; "
                    f"Max-Age={SESSION_TTL}")})
            if route == "logout" and method == "POST":
                app.logout(self.token())
                return self.json(headers={"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0"})

            # ---- Assistent: nur solange nicht eingerichtet
            if not configured:
                return self.api_setup(method, route, query)
            if route.startswith("setup/"):
                raise ApiError("This device is already set up.", 403)

            # ---- alles Weitere nur angemeldet
            if not self.authenticated():
                raise ApiError("Please log in.", 401)
            handler = getattr(self, "api_" + method.lower() + "_" + re.sub(r"[^a-z]", "_", route),
                              None)
            if not handler:
                raise ApiError("Unknown request.", 404)
            return handler(query) if method == "GET" else handler(self.body())

        # -- Assistent
        def api_setup(self, method, route, query):
            if route == "setup/state":
                return self.json(app.setup_state(self.via_hotspot()))
            if route == "snapshot":
                img = app.snapshot(query.get("cam", [""])[0])
                return self.send(200, img, "image/jpeg") if img else self.send(503)
            if method != "POST":
                raise ApiError("Unknown request.", 404)
            data = self.body()
            if route == "setup/cameras":
                app.save_cameras(data.get("cameras"), wizard=True)
                return self.json({"ok": True, "hostname": app.hostname()})
            if route == "setup/scan":
                fresh = app.scan(str(data.get("iface") or "") or None)
                return self.json({"networks": app.networks_json(), "fresh": fresh})
            if route == "setup/finish":
                app.setup_finish(data)
                return self.json()
            raise ApiError("Unknown request.", 404)

        # -- Übersicht
        def api_get_status(self, _):
            conf = load_conf(CONF)
            cams = camera.overview()
            ifaces = netlib.interfaces()
            stats = system.stats()
            active = [c for c in cams if c["enabled"]]
            warnings = []
            if stats["undervoltage"]:
                warnings.append("The power supply is too weak (undervoltage). Use the official "
                                "5 V / 3 A power supply – otherwise the camera may fail.")
            elif stats["undervoltage_seen"]:
                warnings.append("Undervoltage was detected since the last start. A stronger "
                                "power supply is recommended.")
            if stats["cpu_temp"] and stats["cpu_temp"] >= 80:
                warnings.append(f"The Pi is very hot ({stats['cpu_temp']} °C). Add a heatsink or "
                                "improve ventilation.")
            if stats["disk_percent"] and stats["disk_percent"] >= 95:
                warnings.append("The SD card is almost full.")
            rec = record.read_status()
            if rec and rec.get("mode") != "off" and rec.get("error"):
                warnings.append("Recording is not running: " + rec["error"])
            for c in active:
                if not c["present"]:
                    warnings.append(f"Camera “{c['name']}” is not connected.")
            if not cams:
                warnings.append("No USB camera found. Plug one in and open “Cameras”.")
            if any(c["new"] for c in cams):
                warnings.append("A new USB camera was found. Enable it under “Cameras”.")
            self.json({
                "name": conf.get("CAM_NAME", "OnviPi"), "hostname": app.hostname(),
                "version": system.version(), "ip": primary_ip(), "stats": stats,
                "warnings": warnings,
                "cameras": [{k: c[k] for k in ("id", "name", "enabled", "present", "new",
                                               "size", "fps")} for c in cams],
                "interfaces": ifaces, "hotspot": app.ap,
                "recording": {"mode": (rec or {}).get("mode", "off"),
                              "active": (rec or {}).get("recording", [])},
                "terminal": app.terminal.state(), "ip_change": app.ip_change.state()})

        def api_get_snapshot(self, query):
            img = app.snapshot(query.get("cam", [""])[0])
            return self.send(200, img, "image/jpeg") if img else self.send(503)

        # -- Kameras
        def api_get_cameras(self, _):
            self.json({"cameras": camera.overview(), "max_cameras": camera.MAX_CAMERAS,
                       "ip": primary_ip()})

        def api_post_cameras(self, data):
            app.save_cameras(data.get("cameras"))
            self.json()

        def api_post_cameras_remove(self, data):
            app.remove_camera(str(data.get("id") or ""))
            self.json()

        # -- Netzwerk
        def api_get_network(self, _):
            conf = load_conf(CONF)
            self.json({
                "interfaces": netlib.interfaces(), "wifi_devices": app.net.refresh(),
                "wifi_iface": app.net.iface, "saved": app.net.saved_wifi(),
                "networks": app.networks_json(), "hostname": app.hostname(),
                "country": conf.get("WIFI_COUNTRY", "DE"), "countries": system.countries(),
                "hotspot": app.ap, "ap_ssid": app.net.ap_ssid,
                "stays_connected": app.net.independent,
                "wifi_status": app.wifi_status, "ip_change": app.ip_change.state(),
                "rollback_seconds": netlib.ROLLBACK_SECONDS})

        def api_post_network_scan(self, data):
            fresh = app.scan(str(data.get("iface") or "") or None)
            self.json({"networks": app.networks_json(), "fresh": fresh})

        def api_post_network_wifi(self, data):
            wifi = app.check_wifi(data)
            app.select_wifi_iface(wifi["iface"])
            with app.lock:
                app.wifi_job = (wifi["ssid"], wifi["psk"], wifi["hidden"])
                app.wifi_status = {"phase": "connecting", "ssid": wifi["ssid"], "error": ""}
            app.wake.set()
            self.json()

        def api_get_network_wifi_status(self, _):
            self.json(app.wifi_status)

        def api_post_network_forget(self, data):
            if not app.net.forget(str(data.get("name") or "")):
                raise ApiError("Could not remove this network.")
            self.json()

        def api_get_network_ip(self, query):
            connection = query.get("connection", [""])[0]
            if connection not in [d["connection"] for d in netlib.devices() if d["connection"]]:
                raise ApiError("Unknown connection.")
            self.json(netlib.ip_config(connection))

        def api_post_network_ip(self, data):
            connection = str(data.get("connection") or "")
            active = [d["connection"] for d in netlib.devices()
                      if d["connection"] and d["connection"] != netlib.HOTSPOT]
            if connection not in active:
                raise ApiError("This connection is not active.")
            cfg, err = netlib.validate_ip_config(data)
            if err:
                raise ApiError(err)
            err = app.ip_change.start(connection, cfg)
            if err:
                raise ApiError(err)
            self.json({"ok": True, "seconds": netlib.ROLLBACK_SECONDS,
                       "address": cfg["address"]})

        def api_post_network_ip_confirm(self, _):
            self.json({"ok": True, "confirmed": app.ip_change.confirm()})

        # -- Home Assistant
        def api_get_homeassistant(self, _):
            conf = load_conf(CONF)
            mqtt = load_conf(MQTT_CONF)
            cams = [c for c in camera.load_cameras(conf) if c["enabled"]]
            self.json({
                "user": conf.get("CAM_USER", "admin"), "ip": primary_ip(),
                "hostname": app.hostname(),
                "cameras": [{"id": c["id"], "name": c["name"], "port": c["port"],
                             "onvif_name": re.sub(r"[^A-Za-z0-9_.-]+", "_", c["name"])}
                            for c in cams],
                "mqtt": {"host": mqtt.get("MQTT_HOST", ""), "port": mqtt.get("MQTT_PORT", "1883"),
                         "user": mqtt.get("MQTT_USER", ""),
                         "has_password": bool(mqtt.get("MQTT_PASSWORD")),
                         "snapshot_interval": mqtt.get("SNAPSHOT_INTERVAL", "30")}})

        def api_post_mqtt(self, data):
            old = load_conf(MQTT_CONF)
            host, user = str(data.get("host") or "").strip(), str(data.get("user") or "").strip()
            if not host and not user:
                save_conf(MQTT_CONF, {}, mode=0o600)   # MQTT aus
            else:
                if not re.fullmatch(r"[A-Za-z0-9._-]{1,253}", host) or not user:
                    raise ApiError("Please enter the broker address and a username.")
                try:
                    port = int(data.get("port") or 1883)
                    interval = max(0, min(3600, int(data.get("snapshot_interval") or 0)))
                    if not 1 <= port <= 65535:
                        raise ValueError
                except (TypeError, ValueError):
                    raise ApiError("Please enter a valid port and interval.")
                save_conf(MQTT_CONF, {
                    "MQTT_HOST": host, "MQTT_PORT": str(port), "MQTT_USER": user,
                    "MQTT_PASSWORD": str(data.get("password") or old.get("MQTT_PASSWORD", "")),
                    "SNAPSHOT_INTERVAL": str(interval)}, mode=0o600)
            restart_services("onvipi-mqtt.service")
            self.json()

        # -- System
        def api_get_system(self, _):
            conf = load_conf(CONF)
            self.json({
                "user": conf.get("CAM_USER", "admin"),
                "login_user": conf.get("LOGIN_USER", ""),
                "system_users": system.users_with_password() or [],
                "password_hint": system.PASSWORD_HINT,
                "timezone": system.current_timezone(), "timezones": system.timezones(),
                "hostname": app.hostname(), "ssh": system.ssh_enabled(),
                "terminal": app.terminal.state(), "version": system.version()})

        def api_post_system_password(self, data):
            app.change_password(self.token(), data.get("current", ""), data.get("password", ""))
            self.json()

        def api_post_system_settings(self, data):
            conf = load_conf(CONF)
            tz = data.get("timezone")
            if tz and tz != system.current_timezone():
                if tz not in system.timezones():
                    raise ApiError("Please select a time zone.")
                system.set_timezone(tz)
                restart_services("onvipi-go2rtc.service")   # Uhrzeit im Bild
            country = str(data.get("country") or "").upper()
            if country and country != conf.get("WIFI_COUNTRY"):
                if country not in {c for c, _ in system.countries()}:
                    raise ApiError("Please select a Wi-Fi country.")
                system.set_country(country)
                conf["WIFI_COUNTRY"] = country
            host = str(data.get("hostname") or "").strip().lower()
            if host and host != app.hostname():
                if not HOSTNAME_RE.fullmatch(host):
                    raise ApiError("Hostname: lowercase letters, digits and hyphens only.")
                system.set_hostname(host)
                conf.update(HOSTNAME=host, HOSTNAME_AUTO="0")
            save_conf(CONF, conf)
            self.json({"ok": True, "hostname": app.hostname()})

        def api_post_system_ssh(self, data):
            if not system.set_ssh(bool(data.get("enable"))):
                raise ApiError("Could not change SSH – see the log.")
            log.info("SSH %s via web interface", "enabled" if data.get("enable") else "disabled")
            self.json({"ok": True, "ssh": system.ssh_enabled()})

        def api_post_system_terminal(self, data):
            if data.get("enable"):
                if not app.terminal.available():
                    raise ApiError("The terminal (ttyd) is not installed on this device.")
                err = app.terminal.start(data.get("minutes") or 30)
                if err:
                    raise ApiError(err)
                log.info("Terminal enabled via web interface")
            else:
                app.terminal.stop()
            self.json({"ok": True, "terminal": app.terminal.state()})

        def api_post_system_restart(self, _):
            restart_services()
            self.json()

        def api_post_system_reboot(self, _):
            log.info("Reboot requested via web interface")
            threading.Timer(1.5, system.reboot).start()
            self.json()

        def api_get_system_logs(self, _):
            name = f"onvipi-log-{time.strftime('%Y%m%d-%H%M')}.txt"
            self.send(200, system.logs().encode("utf-8", "replace"), "text/plain; charset=utf-8",
                      {"Content-Disposition": f'attachment; filename="{name}"'})

        # -- Aufnahmen
        @staticmethod
        def rec_settings():
            mode, uuid, days = record.settings(load_conf(CONF))
            record.use(uuid)
            return mode, uuid, days

        def api_get_recordings(self, query):
            mode, uuid, days = self.rec_settings()
            status = record.read_status()
            ready = bool(uuid) and (record.is_local() or record.mounted_uuid() == uuid)
            cam_id, day = query.get("cam", [""])[0], query.get("day", [""])[0]
            if cam_id:
                if not re.fullmatch(r"[a-z0-9]+", cam_id) or not re.fullmatch(r"\d{8}", day):
                    raise ApiError("Invalid request.")
                files = record.list_files(cam_id) if ready else []
                return self.json({"files": [
                    {"name": name, "start": int(start), "size": size}
                    for start, name, size in files if name.startswith(day)]})
            cams = []
            for cam in camera.load_cameras():
                counts = {}
                for _, name, size in (record.list_files(cam["id"]) if ready else []):
                    d = counts.setdefault(name[:8], [0, 0])
                    d[0] += 1
                    d[1] += size
                cams.append({"id": cam["id"], "name": cam["name"], "motion": cam["motion"],
                             "days": [{"day": k, "count": v[0], "size": v[1]}
                                      for k, v in sorted(counts.items(), reverse=True)]})
            self.json({"settings": {"mode": mode, "uuid": uuid, "days": days},
                       "storages": record.storages(), "status": status, "ready": ready,
                       "cameras": cams})

        def api_post_recordings_settings(self, data):
            mode, uuid = str(data.get("mode") or "off"), str(data.get("uuid") or "")
            if mode not in record.MODES:
                raise ApiError("Please choose a recording mode.")
            if mode != "off" and uuid not in [s["uuid"] for s in record.storages()]:
                raise ApiError("Please choose where to store the recordings.")
            try:
                days = max(1, min(365, int(data.get("days") or 14)))
            except (TypeError, ValueError):
                raise ApiError("Please enter the number of days to keep recordings.")
            conf = load_conf(CONF)
            conf.update(REC_MODE=mode, REC_UUID=uuid, REC_DAYS=str(days))
            save_conf(CONF, conf)
            log.info("Recording: %s on %s, keep %d days", mode, uuid or "-", days)
            restart_services("onvipi-record.service")
            self.json()

        def rec_path(self, cam_id, name):
            if not re.fullmatch(r"[a-z0-9]+", cam_id or "") or record.start_of(name or "") is None:
                return None
            self.rec_settings()
            path = os.path.join(record.camera_dir(cam_id), name)
            return path if os.path.isfile(path) else None

        def api_post_recordings_delete(self, data):
            """Löscht einzelne Aufnahmen ({cam, files: [...]}) oder einen ganzen Tag ({cam, day})."""
            cam_id = str(data.get("cam") or "")
            names = data.get("files") if isinstance(data.get("files"), list) else []
            day = str(data.get("day") or "")
            if re.fullmatch(r"\d{8}", day) and re.fullmatch(r"[a-z0-9]+", cam_id):
                self.rec_settings()
                names = [n for _, n, _ in record.list_files(cam_id) if n.startswith(day)]
            deleted = 0
            for name in names[:5000]:
                path = self.rec_path(cam_id, str(name))
                if path:
                    record.remove(path)
                    deleted += 1
            if not deleted:
                raise ApiError("These recordings no longer exist.", 404)
            self.json({"ok": True, "deleted": deleted})

        def recording(self, rest, download):
            """Aufnahme ausliefern – mit Range-Unterstützung, damit man im Video springen kann."""
            if not system.configured() or not self.authenticated():
                return self.redirect("/")
            cam_id, _, name = rest.partition("/")
            if name.endswith(".jpg"):   # Vorschaubild für die Galerie
                path = self.rec_path(cam_id, name[:-4] + ".mp4")
                with app.thumbs:
                    img = record.thumbnail(path) if path else None
                if not img:
                    return self.send(404, b"Not found\n", "text/plain")
                return self.send(200, img, "image/jpeg", cache=True)
            path = self.rec_path(cam_id, name)
            if not path:
                return self.send(404, b"Not found\n", "text/plain")
            size = os.path.getsize(path)
            start, end = 0, size - 1
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
            partial = bool(m and (m.group(1) or m.group(2)))
            if partial:
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    return self.end_headers()
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "private, no-store")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{cam_id}-{name}"')
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        # -- Update, Sicherung, Werksreset
        def api_get_system_update(self, _):
            self.json({"current": system.version(), "status": update.read_status()})

        def api_post_system_update_check(self, _):
            try:
                self.json(update.check())
            except (OSError, ValueError) as ex:
                log.info("Update check failed: %s", ex)
                raise ApiError("Could not reach the update server. Is the camera connected "
                               "to the internet?", 502)

        def api_post_system_update_install(self, _):
            err = update.start()
            if err:
                raise ApiError(err)
            log.info("Update started via web interface")
            self.json()

        def api_get_system_backup(self, _):
            name = f"onvipi-backup-{app.hostname()}-{time.strftime('%Y%m%d')}.json"
            self.send(200, json.dumps(system.backup(), indent=2).encode(), "application/json",
                      {"Content-Disposition": f'attachment; filename="{name}"'})

        def api_post_system_restore(self, data):
            err = system.restore(data)
            if err:
                raise ApiError(err)
            log.info("Settings restored from a backup")
            app.logout(self.token(), others=True)
            restart_services(*SERVICES, "onvipi-record.service")
            self.json({"ok": True, "hostname": app.hostname()})

        def api_post_system_reset(self, data):
            expected = load_conf(CONF).get("CAM_PASSWORD", "")
            if not hmac.compare_digest(str(data.get("password", "")).encode(), expected.encode()):
                raise ApiError("The password is not correct.", 403)
            log.warning("Factory reset requested via web interface")
            threading.Timer(1.5, system.factory_reset).start()
            self.json()

        # -- Terminal (ttyd auf 127.0.0.1, inkl. WebSocket)
        def terminal_proxy(self):
            if not system.configured() or not self.authenticated():
                return self.redirect("/")
            if not app.terminal.active():
                return self.send(503, b"The terminal is switched off.\n", "text/plain")
            upgrade = "upgrade" in (self.headers.get("Connection") or "").lower()
            if upgrade and not self.same_origin():
                return self.send(403, b"Forbidden\n", "text/plain")
            try:
                backend = socket.create_connection(("127.0.0.1", system.Terminal.PORT), timeout=5)
            except OSError:
                return self.send(503, b"The terminal is starting - please reload.\n",
                                 "text/plain")
            self.close_connection = True
            client = self.connection
            try:
                head = [self.requestline]
                for key, value in self.headers.items():
                    if key.lower() in ("cookie", "connection", "proxy-connection"):
                        continue
                    head.append(f"{key}: {value}")
                head.append("Connection: Upgrade" if upgrade else "Connection: close")
                length = int(self.headers.get("Content-Length") or 0)
                payload = self.rfile.read(length) if length else b""
                backend.sendall("\r\n".join(head).encode("latin-1") + b"\r\n\r\n" + payload)
                backend.settimeout(None)
                while True:
                    readable, _, _ = select.select([client, backend], [], [], 60)
                    if not readable:
                        if not (app.terminal.active() and app.session_valid(self.token())):
                            break   # Frist abgelaufen oder abgemeldet
                        continue
                    for src, dst in ((client, backend), (backend, client)):
                        if src in readable:
                            data = src.recv(65536)
                            if not data:
                                return
                            dst.sendall(data)
            except OSError:
                pass
            finally:
                backend.close()

    return Handler
