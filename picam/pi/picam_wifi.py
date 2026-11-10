#!/usr/bin/env python3
"""PiCam WLAN-Einrichtung mit Captive Portal (NetworkManager).

Ablauf:
  * Beim Start bis zu BOOT_GRACE Sekunden auf eine Netzwerkverbindung warten.
  * Keine Verbindung -> Hotspot "AP_SSID" mit Captive Portal auf http://10.42.0.1
  * Im Portal (Schritt-für-Schritt-Wizard): Kameraname + USB-Gerät wählen, Vorschau
    prüfen, dann WLAN auswählen + Passwort.
  * Verbindung klappt -> Hotspot aus. Klappt nicht -> Hotspot wieder an, Fehler im Portal.
  * Ist im Hotspot-Modus niemand verbunden, wird alle RETRY_INTERVAL Sekunden
    erneut das gespeicherte WLAN versucht (z. B. nach Stromausfall, wenn der Router
    langsamer bootet als der Pi).
  * Fällt das WLAN später für OFFLINE_GRACE Sekunden aus, startet der Hotspot erneut.
  * Im Normalbetrieb läuft auf demselben Port die Einstellungsseite (http://<pi>/),
    geschützt mit CAM_USER/CAM_PASSWORD – auch für Zugriff über die FRITZ!Box.

Befehle:
  picam_wifi.py run      Dienst (systemd)
  picam_wifi.py status   Zustand anzeigen
  picam_wifi.py reset    gespeicherte WLANs löschen -> Einrichtungsmodus
  picam_wifi.py portal   Hotspot + Portal sofort starten (Test / manuelles Setup,
                         auch wenn der Pi gerade online ist)
"""
import argparse
import base64
import hmac
import html
import http.server
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from picamlib import CONF, load_conf, primary_ip, save_conf  # noqa: E402
from picam_camera import ROTATIONS, list_cameras, rotation  # noqa: E402

log = logging.getLogger("picam-wifi")

HOTSPOT = "picam-hotspot"
AP_IP = "10.42.0.1"
AP_NET = "10.42.0."
PORTAL_PORT = int(os.environ.get("PICAM_PORTAL_PORT", "80"))
LOGIN_MAX_FAILS = 5          # danach ist die Adresse LOGIN_LOCK Sekunden gesperrt
LOGIN_LOCK = 600
PASSWORD_RE = re.compile(r"[A-Za-z0-9._~!*+=-]{8,64}")  # muss in die RTSP-URL passen
BOOT_GRACE = 60
OFFLINE_GRACE = 300
RETRY_INTERVAL = 300
CONNECT_TIMEOUT = 45

FMT_LABEL = {"mjpeg": "MJPEG", "h264": "H.264", "yuyv422": "YUYV (unkomprimiert)"}
ROTATE_LABEL = {0: "Normal", 90: "Hochformat (90° rechts)", 180: "Kopfüber (180°)",
                270: "Hochformat (90° links)"}
STEP_NAMES = ["Start", "Kamera", "Vorschau", "WLAN"]


# --------------------------------------------------------------------------- nmcli
def run(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


def nmcli(*args, timeout=30):
    r = run(["nmcli", *args], timeout=timeout)
    if r.returncode != 0:
        log.debug("nmcli %s -> %s %s", " ".join(args), r.returncode, r.stderr.strip())
    return r


def split_terse(line):
    """nmcli -t trennt mit ':' und maskiert ':' in Werten als '\\:'."""
    return [p.replace("\\:", ":").replace("\\\\", "\\") for p in re.split(r"(?<!\\):", line)]


class Net:
    def __init__(self, conf):
        self.iface = conf.get("WIFI_IFACE", "wlan0")
        self.ap_ssid = conf.get("AP_SSID", "PiCam-Setup")
        self.ap_password = conf.get("AP_PASSWORD", "")

    def active(self):
        out = nmcli("-t", "-f", "NAME,TYPE,DEVICE", "con", "show", "--active").stdout
        return [split_terse(line) for line in out.splitlines() if line]

    def online(self):
        for fields in self.active():
            if len(fields) >= 2 and fields[0] != HOTSPOT and fields[1] in (
                    "802-11-wireless", "802-3-ethernet", "wifi", "ethernet"):
                return True
        return False

    def hotspot_active(self):
        return any(f and f[0] == HOTSPOT for f in self.active())

    def saved_wifi(self):
        out = nmcli("-t", "-f", "NAME,TYPE", "con", "show").stdout
        return [f[0] for f in map(split_terse, out.splitlines())
                if len(f) >= 2 and f[1] in ("802-11-wireless", "wifi") and f[0] != HOTSPOT]

    def scan(self):
        nmcli("dev", "wifi", "rescan", "ifname", self.iface, timeout=20)
        time.sleep(4)
        out = nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list",
                    "ifname", self.iface, timeout=20).stdout
        best = {}
        for fields in map(split_terse, out.splitlines()):
            if len(fields) < 3 or not fields[0] or fields[0] == self.ap_ssid:
                continue
            ssid, signal_, sec = fields[0], int(fields[1] or 0), fields[2]
            if ssid not in best or best[ssid][0] < signal_:
                best[ssid] = (signal_, sec)
        nets = sorted(((s, v[0], v[1]) for s, v in best.items()), key=lambda n: -n[1])
        log.info("Scan: %d Netzwerke", len(nets))
        return nets

    def ensure_hotspot_profile(self):
        nmcli("con", "delete", HOTSPOT)  # immer frisch aus picam.conf anlegen
        args = ["con", "add", "type", "wifi", "ifname", self.iface, "con-name", HOTSPOT,
                "autoconnect", "no", "ssid", self.ap_ssid,
                "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
                "802-11-wireless.channel", "6",
                "ipv4.method", "shared", "ipv4.addresses", f"{AP_IP}/24",
                "ipv6.method", "disabled"]
        if self.ap_password:
            # rsn/ccmp + PMF aus: nötig, damit der Pi-WLAN-Chip im AP-Modus zuverlässig läuft
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", self.ap_password,
                     "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp",
                     "wifi-sec.group", "ccmp", "wifi-sec.pmf", "disable"]
        return nmcli(*args).returncode == 0

    def hotspot_up(self):
        self.ensure_hotspot_profile()
        ok = nmcli("con", "up", HOTSPOT, timeout=40).returncode == 0
        log.info("Hotspot '%s' %s", self.ap_ssid, "aktiv" if ok else "konnte nicht starten")
        return ok

    def hotspot_down(self):
        nmcli("con", "down", HOTSPOT, timeout=20)

    def clients_connected(self):
        out = run(["iw", "dev", self.iface, "station", "dump"], timeout=5).stdout
        return "Station" in out

    def wait_online(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if self.online():
                return True
            time.sleep(2)
        return False

    def connect_new(self, ssid, psk, hidden):
        name = "wifi-" + re.sub(r"[^A-Za-z0-9_.-]+", "_", ssid)[:40]
        nmcli("con", "delete", name)
        args = ["con", "add", "type", "wifi", "ifname", self.iface, "con-name", name,
                "ssid", ssid, "connection.autoconnect", "yes",
                "connection.autoconnect-priority", "10"]
        if hidden:
            args += ["802-11-wireless.hidden", "yes"]
        if psk:
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk]
        r = nmcli(*args)
        if r.returncode != 0:
            return False, r.stderr.strip() or "Profil konnte nicht angelegt werden"
        self.hotspot_down()
        r = nmcli("-w", str(CONNECT_TIMEOUT), "con", "up", name, timeout=CONNECT_TIMEOUT + 15)
        if r.returncode == 0 and self.wait_online(10):
            log.info("Verbunden mit '%s'", ssid)
            return True, ""
        log.warning("Verbindung zu '%s' fehlgeschlagen: %s", ssid, r.stderr.strip())
        nmcli("con", "delete", name)
        return False, r.stderr.strip()


# --------------------------------------------------------------------------- Portal
CSS = """
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,sans-serif;margin:0;
background:#f4f5f7;color:#1d2330}main{max-width:460px;margin:0 auto;padding:22px 16px}
h1{font-size:1.4rem;margin:0 0 4px}p.sub{color:#5b6475;margin:0 0 16px}
.card{background:#fff;border-radius:14px;padding:18px;box-shadow:0 1px 3px #0001;margin-bottom:14px}
label{display:block;font-weight:600;font-size:.9rem;margin:12px 0 6px}
input,select{width:100%;padding:12px;font-size:1rem;border:1px solid #cfd4dc;border-radius:10px;background:#fff}
button,.btn{display:inline-block;width:100%;padding:14px 16px;font-size:1.05rem;font-weight:600;
border:0;border-radius:10px;background:#1f6feb;color:#fff;text-decoration:none;text-align:center;cursor:pointer}
.btn.sec,button.sec{background:#e7e9ee;color:#1d2330}
.row{display:flex;gap:10px;margin-top:18px}.row>*{margin-top:0;flex:1}
.err{background:#fde8e8;color:#9b1c1c;padding:12px;border-radius:10px;margin-bottom:14px}
small{color:#5b6475}.center{text-align:center}.note{color:#5b6475;margin:0 0 12px}
.hero{font-size:3rem;text-align:center;margin-bottom:6px}
.shot{width:100%;border-radius:10px;background:#000;display:block;min-height:150px;margin-bottom:12px}
.steps{display:flex;align-items:center;gap:6px;margin-bottom:16px;flex-wrap:wrap}
.dot{width:26px;height:26px;border-radius:50%;background:#d7dbe3;color:#5b6475;
display:inline-flex;align-items:center;justify-content:center;font-size:.85rem;font-weight:700}
.dot.on{background:#1f6feb;color:#fff}.dot.done{background:#9bc0ff;color:#0b2e66}
.stepname{margin-left:4px;font-weight:600;color:#5b6475;font-size:.9rem}
.ok{background:#e3f5e8;color:#14532d;padding:12px;border-radius:10px;margin-bottom:14px}
h2{font-size:1.1rem;margin:0 0 8px}ol{padding-left:20px;margin:8px 0}li{margin:4px 0}
code{background:#eef0f4;padding:2px 6px;border-radius:6px;font-size:.95rem;word-break:break-all}
table.kv{width:100%;border-collapse:collapse;margin-top:8px}
table.kv td{padding:6px 0;border-bottom:1px solid #eef0f4}table.kv td:first-child{color:#5b6475;width:40%}
.check{display:flex;gap:10px;align-items:center;font-weight:600;margin-top:12px}
.check input{width:auto}input[type=range]{padding:0}
"""

JS_CAMERA = """<script>
var CAPS=__CAPS__,CURRENT=__CURRENT__,FMT=__FMT__;
function $(id){return document.getElementById(id);}
var dev=$("device"),fmt=$("format"),size=$("size"),fps=$("fps");
function fill(sel,items,cur){sel.innerHTML="";items.forEach(function(it){
 var o=document.createElement("option");o.value=it[0];o.textContent=it[1];
 if(String(it[0])===String(cur))o.selected=true;sel.appendChild(o);});}
function onDev(c){var f=CAPS[dev.value]||{};
 fill(fmt,Object.keys(f).map(function(k){return [k,FMT[k]||k];}),c&&c.format);onFmt(c);}
function onFmt(c){var s=(CAPS[dev.value]||{})[fmt.value]||{};
 fill(size,Object.keys(s).map(function(k){return [k,k];}),c&&c.size);onRes(c);}
function onRes(c){var l=((CAPS[dev.value]||{})[fmt.value]||{})[size.value]||[];
 fill(fps,l.map(function(x){return [x,x+" fps"];}),c&&c.fps);}
dev.onchange=function(){onDev(null);};fmt.onchange=function(){onFmt(null);};
size.onchange=function(){onRes(null);};
onDev(CURRENT);
</script>"""


def page(title, body):
    return (f"<!doctype html><html lang=de><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
            f"<body><main>{body}</main></body></html>").encode()


def hostname(conf):
    return socket.gethostname() or conf.get("HOSTNAME", "picam")


def ha_card(conf, host=None):
    """Anleitung: Kamera per ONVIF in Home Assistant hinzufügen (inkl. Zugangsdaten)."""
    e = html.escape
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", conf.get("CAM_NAME", "PiCam")) or "PiCam"
    host = host or f"{hostname(conf)}.local"
    return (f"<div class=card><h2>🏠 In Home Assistant hinzufügen</h2>"
            f"<p class=note>Livebild und Bewegungssensor kommen per ONVIF. "
            f"Home Assistant zeigt ONVIF-Kameras <b>nicht</b> von selbst an – einmalig hinzufügen:</p>"
            f"<ol><li><b>Einstellungen → Geräte &amp; Dienste</b></li>"
            f"<li><b>Integration hinzufügen → ONVIF</b></li>"
            f"<li>Gerät <b>„{e(name)}“</b> auswählen</li>"
            f"<li>Benutzer und Passwort eingeben:</li></ol>"
            f"<table class=kv><tr><td>Benutzer</td><td><code>{e(conf.get('CAM_USER', 'admin'))}</code></td></tr>"
            f"<tr><td>Passwort</td><td><code>{e(conf.get('CAM_PASSWORD', ''))}</code></td></tr></table>"
            f"<p><small>Erscheint die Kamera nicht in der Liste: „Manuell konfigurieren“ mit "
            f"Host <code>{e(host)}</code> und Port <code>{e(conf.get('ONVIF_PORT', '8000'))}</code>.</small></p>"
            f"</div>")


class Portal:
    def __init__(self, conf):
        self.conf = conf
        self.lock = threading.Lock()
        self.networks = []
        self.error = ""
        self.pending = None
        self.server = None
        self.ap = False          # Hotspot aktiv -> Hotspot-Clients bekommen den Assistenten
        self.fails = {}          # IP -> (Fehlversuche, Zeitpunkt des letzten)

    # -- Anmeldung (Einstellungsseite)
    def locked(self, ip):
        n, since = self.fails.get(ip, (0, 0))
        if n >= LOGIN_MAX_FAILS and time.time() - since < LOGIN_LOCK:
            return True
        if n and time.time() - since >= LOGIN_LOCK:
            self.fails.pop(ip, None)
        return False

    def check_login(self, ip, header):
        conf = load_conf(CONF)
        user, pw = conf.get("CAM_USER", "admin"), conf.get("CAM_PASSWORD", "")
        try:
            got_user, _, got_pw = base64.b64decode(header.split(" ", 1)[1]).decode().partition(":")
        except (IndexError, ValueError, UnicodeDecodeError):
            got_user = got_pw = None
        ok = bool(pw) and got_user is not None and hmac.compare_digest(
            f"{got_user}:{got_pw}".encode(), f"{user}:{pw}".encode())
        if ok:
            self.fails.pop(ip, None)
        elif header:
            n = self.fails.get(ip, (0, 0))[0] + 1
            self.fails[ip] = (n, time.time())
            log.warning("Fehlgeschlagene Anmeldung von %s (%d)", ip, n)
        return ok

    # -- Gerüst
    def _wrap(self, step, body):
        dots = ""
        for i in range(len(STEP_NAMES)):
            cls = "dot on" if i + 1 == step else ("dot done" if i + 1 < step else "dot")
            dots += f"<span class='{cls}'>{i + 1}</span>"
        bar = (f"<div class=steps>{dots}<span class=stepname>Schritt {step}/{len(STEP_NAMES)}"
               f" · {html.escape(STEP_NAMES[step - 1])}</span></div>")
        return bar + body

    # -- Seiten
    def welcome(self):
        name = html.escape(load_conf(CONF).get("CAM_NAME", "PiCam"))
        body = (f"<div class=card><div class=hero>📷</div><h1>Kamera einrichten</h1>"
                f"<p class=sub>Dieser Assistent richtet „{name}“ für Home Assistant ein: "
                f"Name vergeben, USB-Kamera wählen, kurz prüfen und ins WLAN bringen.</p>"
                f"<a class=btn href=/camera>Los geht’s →</a></div>")
        return page("Willkommen", self._wrap(1, body))

    def camera(self, error=""):
        e = html.escape
        fields = self.camera_fields()
        if not fields:
            body = ("<div class=card><h1>Keine USB-Kamera gefunden</h1>"
                    "<p class=note>Bitte die Webcam anstecken und neu laden.</p>"
                    "<a class=btn href=/camera>Neu laden</a></div>")
            return page("Keine Kamera", self._wrap(2, body))
        err = f"<div class=err>{e(error)}</div>" if error else ""
        body = (f"{err}<form method=post action=/camera><div class=card>{fields[0]}</div>"
                f"<div class=row><a class='btn sec' href=/>← Zurück</a>"
                f"<button>Weiter →</button></div></form>{fields[1]}")
        return page("Kamera", self._wrap(2, body))

    @staticmethod
    def camera_fields():
        """Eingabefelder Name/Gerät/Format/Auflösung/Bildrate + Script, None ohne Kamera."""
        e = html.escape
        conf = load_conf(CONF)
        cams = list_cameras()
        if not cams:
            return None
        cur = conf.get("VIDEO_DEVICE", "")
        devsel = "".join(
            f"<option value=\"{e(c['dev'])}\"{' selected' if c['dev'] == cur else ''}>"
            f"{e(c['label'])}</option>" for c in cams)
        caps = {c["dev"]: c["formats"] for c in cams}
        current = {"format": conf.get("VIDEO_FORMAT", ""), "size": conf.get("VIDEO_SIZE", ""),
                   "fps": conf.get("FRAMERATE", "")}
        js = (JS_CAMERA.replace("__CAPS__", json.dumps(caps))
              .replace("__CURRENT__", json.dumps(current))
              .replace("__FMT__", json.dumps(FMT_LABEL)))
        fields = (f"<label for=name>Name der Kamera</label>"
                  f"<input name=name id=name maxlength=48 autocapitalize=words "
                  f"value=\"{e(conf.get('CAM_NAME', 'PiCam'))}\">"
                  f"<label for=device>USB-Kamera</label>"
                  f"<select name=device id=device>{devsel}</select>"
                  f"<label for=format>Format</label><select name=format id=format></select>"
                  f"<label for=size>Auflösung</label><select name=size id=size></select>"
                  f"<label for=fps>Bildrate</label><select name=fps id=fps></select>"
                  f"<label for=rotate>Ausrichtung</label><select name=rotate id=rotate>"
                  + "".join(f"<option value={r}{' selected' if r == rotation(conf) else ''}>"
                            f"{ROTATE_LABEL[r]}</option>" for r in ROTATIONS)
                  + "</select>"
                  + "<label class=check><input type=checkbox name=overlay value=1"
                  + (" checked" if conf.get("OVERLAY_TIME", "1") == "1" else "")
                  + "> Datum/Uhrzeit unten rechts einblenden</label>")
        return fields, js

    def preview(self):
        name = html.escape(load_conf(CONF).get("CAM_NAME", "PiCam"))
        body = (f"<div class=card><p class=note>Ist „{name}“ die richtige Kamera? "
                f"Das Bild wird frisch vom Gerät geholt.</p>"
                f"<img id=shot class=shot src=/snapshot.jpg alt='Vorschau' "
                f"onerror=\"this.style.background='#333'\">"
                f"<button type=button class='btn sec' "
                f"onclick=\"document.getElementById('shot').src='/snapshot.jpg?x='+Date.now()\">"
                f"🔄 Aktualisieren</button></div>"
                f"<div class=row><a class='btn sec' href=/camera>← Zurück</a>"
                f"<a class=btn href=/wifi>Passt →</a></div>")
        return page("Vorschau", self._wrap(3, body))

    def wifi(self):
        e = html.escape
        opts = "".join(
            f"<option value=\"{e(s)}\">{e(s)} &nbsp;({sig}%{' 🔒' if sec and sec != '--' else ''})"
            f"</option>" for s, sig, sec in self.networks)
        opts += "<option value=''>Anderes / verstecktes Netzwerk …</option>"
        err = f"<div class=err>{e(self.error)}</div>" if self.error else ""
        body = (f"{err}<form method=post action=/connect><div class=card>"
                f"<label for=ssid>WLAN</label>"
                f"<select name=ssid id=ssid onchange=\"document.getElementById('m')"
                f".style.display=this.value?'none':'block'\">{opts}</select>"
                f"<div id=m style=\"display:{'none' if self.networks else 'block'}\">"
                f"<label for=manual>Netzwerkname (SSID)</label>"
                f"<input name=manual id=manual autocapitalize=off></div>"
                f"<label for=psk>WLAN-Passwort</label>"
                f"<input type=password name=psk id=psk autocomplete=off minlength=8 maxlength=63>"
                f"<small>Leer lassen bei offenem WLAN.</small></div>"
                f"<div class=row><a class='btn sec' href=/preview>← Zurück</a>"
                f"<button>Verbinden →</button></div></form>"
                f"<p class=center><a href=/done>WLAN überspringen (Pi bleibt am Kabel)</a></p>")
        return page("WLAN", self._wrap(4, body))

    def connecting(self, ssid):
        e = html.escape
        conf = load_conf(CONF)
        host = e(hostname(conf))
        body = (f"<div class=card><div class=hero>⏳</div><h1>Verbinde mit „{e(ssid)}“</h1>"
                f"<p>Das Einrichtungs-WLAN wird jetzt abgeschaltet – dein Handy wechselt zurück "
                f"ins normale WLAN.</p>"
                f"<p><b>Hat es geklappt?</b> Nach ca. 1 Minute erreichst du die Einstellungen "
                f"unter <b>http://{host}.local</b>.</p>"
                f"<p>Taucht das WLAN <b>{e(conf.get('AP_SSID', ''))}</b> wieder auf, war das "
                f"Passwort falsch – einfach erneut verbinden.</p>"
                f"<p class=note>📸 Mach am besten jetzt ein Bildschirmfoto der Zugangsdaten unten.</p>"
                f"</div>{ha_card(conf)}")
        return page("Verbinde …", self._wrap(4, body))

    def done(self):
        e = html.escape
        conf = load_conf(CONF)
        host, name = e(hostname(conf)), e(conf.get("CAM_NAME", "PiCam"))
        body = (f"<div class=card><div class=hero>✅</div><h1>Fertig</h1>"
                f"<p>„{name}“ ist eingerichtet. Du bleibst über das Kabel verbunden.</p>"
                f"<p><b>Einstellungen:</b> http://{host}.local</p></div>{ha_card(conf)}")
        return page("Fertig", self._wrap(4, body))

    # -- Einstellungsseite (Normalbetrieb, mit Anmeldung)
    MESSAGES = {
        "camera": "Kamera-Einstellungen gespeichert, Stream wurde neu gestartet.",
        "motion": "Bewegungserkennung gespeichert.",
        "password": "Passwort geändert. Bitte in Home Assistant bei der ONVIF-Kamera "
                    "„Neu konfigurieren“ und das neue Passwort eintragen.",
        "restart": "Dienste werden neu gestartet …",
    }

    def settings(self, msg="", error=""):
        e = html.escape
        conf = load_conf(CONF)
        ip = primary_ip() or ""
        host = hostname(conf)
        note = (f"<div class=err>{e(error)}</div>" if error
                else f"<div class=ok>{e(self.MESSAGES[msg])}</div>" if msg in self.MESSAGES else "")
        conn = ", ".join(f[0] for f in Net(conf).active()
                         if len(f) >= 3 and f[1] != "loopback") or "–"

        fields = self.camera_fields()
        cam = (f"<form method=post action=/settings/camera><div class=card><h2>📷 Kamera</h2>"
               f"{fields[0]}<div class=row><button>Speichern</button></div></div></form>{fields[1]}"
               if fields else "<div class=card><h2>📷 Kamera</h2>"
               "<p class=err>Keine USB-Kamera gefunden.</p></div>")

        try:
            sens = max(0, min(100, int(conf.get("MOTION_SENSITIVITY", "60"))))
        except ValueError:
            sens = 60
        checked = " checked" if conf.get("MOTION_ENABLE", "1") == "1" else ""
        motion = (f"<form method=post action=/settings/motion><div class=card>"
                  f"<h2>🟢 Bewegungserkennung</h2>"
                  f"<label class=check><input type=checkbox name=enable value=1{checked}> aktiv</label>"
                  f"<label for=sens>Empfindlichkeit: <span id=sv>{sens}</span></label>"
                  f"<input type=range name=sensitivity id=sens min=0 max=100 value={sens} "
                  f"oninput=\"document.getElementById('sv').textContent=this.value\">"
                  f"<small>Höher = empfindlicher. Löst es ständig aus, verringern.</small>"
                  f"<div class=row><button>Speichern</button></div></div></form>")

        pw = ("<form method=post action=/settings/password><div class=card><h2>🔒 Passwort</h2>"
              "<p class=note>Gilt für diese Seite, ONVIF und RTSP. Danach muss es auch in "
              "Home Assistant geändert werden.</p>"
              "<label for=pw1>Neues Passwort</label>"
              "<input type=password name=pw1 id=pw1 autocomplete=new-password minlength=8 maxlength=64>"
              "<label for=pw2>Wiederholen</label>"
              "<input type=password name=pw2 id=pw2 autocomplete=new-password minlength=8 maxlength=64>"
              "<small>8–64 Zeichen: Buchstaben, Ziffern und . _ ~ ! * + = -</small>"
              "<div class=row><button>Passwort ändern</button></div></div></form>")

        net = (f"<div class=card><h2>📶 Netzwerk</h2><table class=kv>"
               f"<tr><td>IP-Adresse</td><td>{e(ip or '–')}</td></tr>"
               f"<tr><td>Hostname</td><td>{e(host)}.local</td></tr>"
               f"<tr><td>Verbindung</td><td>{e(conn)}</td></tr>"
               f"<tr><td>Livebild</td><td><a href=\"http://{e(ip or host + '.local')}:1984/\">"
               f"go2rtc (Port 1984, nur im Heimnetz)</a></td></tr></table>"
               f"<p><small>WLAN neu einrichten: Pi ohne Netz starten – dann erscheint "
               f"„{e(conf.get('AP_SSID', ''))}“.</small></p>"
               f"<form method=post action=/settings/restart><div class=row>"
               f"<button class=sec>Dienste neu starten</button></div></form></div>")

        body = (f"<h1>{e(conf.get('CAM_NAME', 'PiCam'))}</h1><p class=sub>PiCam-Einstellungen</p>"
                f"{note}<div class=card><img id=shot class=shot src=/snapshot.jpg alt='Vorschau'>"
                f"<button type=button class='btn sec' "
                f"onclick=\"document.getElementById('shot').src='/snapshot.jpg?x='+Date.now()\">"
                f"🔄 Aktualisieren</button></div>"
                f"{ha_card(conf, ip or None)}{cam}{motion}{pw}{net}")
        return page(conf.get("CAM_NAME", "PiCam"), body)

    def apply_motion(self, form):
        conf = load_conf(CONF)
        try:
            sens = max(0, min(100, int(form.get("sensitivity", ["60"])[0])))
        except ValueError:
            return "Ungültige Empfindlichkeit."
        conf.update(MOTION_ENABLE="1" if form.get("enable") else "0",
                    MOTION_SENSITIVITY=str(sens))
        save_conf(CONF, conf)
        run(["systemctl", "restart", "picam-onvif.service"], timeout=30)
        return None

    def apply_password(self, form):
        pw1, pw2 = form.get("pw1", [""])[0], form.get("pw2", [""])[0]
        if pw1 != pw2:
            return "Die Passwörter stimmen nicht überein."
        if not PASSWORD_RE.fullmatch(pw1):
            return "Passwort: 8–64 Zeichen, nur Buchstaben, Ziffern und . _ ~ ! * + = -"
        conf = load_conf(CONF)
        conf["CAM_PASSWORD"] = pw1
        save_conf(CONF, conf)
        log.info("Kamera-Passwort über die Weboberfläche geändert")
        run(["systemctl", "restart", "go2rtc.service", "picam-onvif.service"], timeout=45)
        return None

    # -- Verarbeitung
    def apply_camera(self, form):
        g = lambda k: (form.get(k, [""])[0]).strip()  # noqa: E731
        name = re.sub(r"[\x00-\x1f'\"\\`$]", "", g("name"))[:48].strip() or "PiCam"
        dev, fmt, size, fps = g("device"), g("format"), g("size"), g("fps")
        caps = {c["dev"]: c["formats"] for c in list_cameras()}
        if dev not in caps:
            return "Bitte eine Kamera auswählen."
        if fmt not in caps[dev] or size not in caps[dev].get(fmt, {}):
            return "Ungültige Format- oder Auflösungswahl."
        allowed = caps[dev][fmt][size]
        try:
            fpsi = int(fps)
        except ValueError:
            fpsi = allowed[0] if allowed else 30
        if allowed and fpsi not in allowed:
            fpsi = allowed[0]
        rot = rotation({"ROTATE": g("rotate")})
        conf = load_conf(CONF)
        conf.update(CAM_NAME=name, VIDEO_DEVICE=dev, VIDEO_FORMAT=fmt,
                    VIDEO_SIZE=size, FRAMERATE=str(fpsi), ROTATE=str(rot),
                    OVERLAY_TIME="1" if form.get("overlay") else "0")
        save_conf(CONF, conf)
        self.conf = conf
        log.info("Kamera gesetzt: '%s' %s %s %s@%sfps, %d°", name, dev, fmt, size, fpsi, rot)
        run(["systemctl", "restart", "go2rtc.service", "picam-onvif.service",
             "picam-wsd.service", "picam-mqtt.service"], timeout=45)
        return None

    def submit_wifi(self, form):
        g = lambda k: (form.get(k, [""])[0]).strip()  # noqa: E731
        ssid = g("ssid") or g("manual")
        psk = form.get("psk", [""])[0]
        if not ssid:
            return "Bitte ein WLAN auswählen oder eingeben."
        if psk and not 8 <= len(psk) <= 63:
            return "Das WLAN-Passwort muss 8 bis 63 Zeichen lang sein."
        hidden = ssid not in {n[0] for n in self.networks}
        with self.lock:
            self.pending = (ssid, psk, hidden)
            self.error = ""
        return None

    def snapshot(self):
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:1984/api/frame.jpeg?src=picam", timeout=15) as r:
                return r.read()
        except OSError as ex:
            log.debug("Snapshot fehlgeschlagen: %s", ex)
            return None

    # -- HTTP-Server
    def start(self):
        portal = self

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "PiCam"

            def log_message(self, fmt, *args):
                log.debug("http: " + fmt, *args)

            def send(self, code, body=b"", ctype=None, headers=None):
                self.send_response(code)
                if ctype:
                    self.send_header("Content-Type", ctype)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def page_out(self, body):
                self.send(200, body, "text/html; charset=utf-8")

            def is_portal_host(self):
                host = (self.headers.get("Host") or "").split(":")[0].lower()
                return host in (AP_IP, "localhost", "127.0.0.1") or host.endswith(".local")

            def read_form(self):
                length = min(int(self.headers.get("Content-Length") or 0), 16384)
                return urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"),
                                             keep_blank_values=True)

            def wizard_client(self):
                return portal.ap and self.client_address[0].startswith(AP_NET)

            def authorized(self):
                """Einstellungsseite: HTTP-Anmeldung mit CAM_USER/CAM_PASSWORD."""
                ip = self.client_address[0]
                if portal.locked(ip):
                    self.send(429, "Zu viele Fehlversuche – bitte 10 Minuten warten.\n".encode(),
                              "text/plain; charset=utf-8")
                    return False
                if portal.check_login(ip, self.headers.get("Authorization") or ""):
                    return True
                self.send(401, "Anmeldung erforderlich (Benutzer admin, Kamera-Passwort).\n"
                          .encode(), "text/plain; charset=utf-8",
                          {"WWW-Authenticate": 'Basic realm="PiCam", charset="UTF-8"'})
                return False

            def same_origin(self):
                """Formulare nur von dieser Seite annehmen (Schutz vor fremden Webseiten)."""
                src = self.headers.get("Origin") or self.headers.get("Referer") or ""
                return urllib.parse.urlparse(src).netloc == (self.headers.get("Host") or "")

            def do_GET(self):
                if not self.wizard_client():
                    return self.settings_get()
                # Fremde Adresse (Android/iOS/Windows-Captive-Checks) -> Portal
                if not self.is_portal_host():
                    dest = "/wifi" if portal.error else "/"
                    return self.send(302, headers={"Location": f"http://{AP_IP}{dest}"})
                path = self.path.split("?")[0]
                if path in ("/", "/index.html"):
                    return self.page_out(portal.welcome())
                if path == "/camera":
                    return self.page_out(portal.camera())
                if path == "/preview":
                    return self.page_out(portal.preview())
                if path == "/snapshot.jpg":
                    img = portal.snapshot()
                    return self.send(200, img, "image/jpeg") if img else self.send(503)
                if path == "/wifi":
                    return self.page_out(portal.wifi())
                if path == "/done":
                    return self.page_out(portal.done())
                self.send(302, headers={"Location": "/"})

            def settings_get(self):
                if not self.authorized():
                    return
                parts = urllib.parse.urlparse(self.path)
                if parts.path == "/snapshot.jpg":
                    img = portal.snapshot()
                    return self.send(200, img, "image/jpeg") if img else self.send(503)
                if parts.path != "/":
                    return self.send(302, headers={"Location": "/"})
                msg = urllib.parse.parse_qs(parts.query).get("msg", [""])[0]
                self.page_out(portal.settings(msg))

            def settings_post(self):
                if not self.authorized():
                    return
                if not self.same_origin():
                    return self.send(403, b"Forbidden\n", "text/plain")
                path = self.path.split("?")[0]
                form = self.read_form()
                if path == "/settings/camera":
                    err = portal.apply_camera(form)
                elif path == "/settings/motion":
                    err = portal.apply_motion(form)
                elif path == "/settings/password":
                    err = portal.apply_password(form)
                elif path == "/settings/restart":
                    subprocess.Popen(["systemctl", "restart", "go2rtc.service",
                                      "picam-onvif.service", "picam-wsd.service",
                                      "picam-mqtt.service"])
                    err = None
                else:
                    return self.send(302, headers={"Location": "/"})
                if err:
                    return self.page_out(portal.settings(error=err))
                key = path.rsplit("/", 1)[-1]
                self.send(303, headers={"Location": f"/?msg={key}"})

            def do_POST(self):
                if not self.wizard_client():
                    return self.settings_post()
                path = self.path.split("?")[0]
                if path == "/camera":
                    err = portal.apply_camera(self.read_form())
                    if err:
                        return self.page_out(portal.camera(err))
                    return self.send(303, headers={"Location": "/preview"})
                if path == "/connect":
                    err = portal.submit_wifi(self.read_form())
                    if err:
                        portal.error = err
                        return self.page_out(portal.wifi())
                    ssid = portal.pending[0] if portal.pending else ""
                    return self.page_out(portal.connecting(ssid))
                self.send(302, headers={"Location": "/"})

        http.server.ThreadingHTTPServer.allow_reuse_address = True
        self.server = http.server.ThreadingHTTPServer(("0.0.0.0", PORTAL_PORT), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        log.info("Captive Portal läuft auf Port %d", PORTAL_PORT)

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def take_pending(self):
        with self.lock:
            p, self.pending = self.pending, None
            return p


# --------------------------------------------------------------------------- Ablauf
def restart_mqtt():
    run(["systemctl", "restart", "picam-mqtt.service"], timeout=20)


def ap_mode(net, portal):
    portal.networks = net.scan()  # vor dem Hotspot scannen – im AP-Modus geht das nicht
    portal.error = ""
    net.hotspot_up()
    portal.ap = True
    last_retry = time.time()
    try:
        while True:
            pending = portal.take_pending()
            if pending:
                time.sleep(2)  # Antwortseite noch ausliefern lassen
                ok, err = net.connect_new(*pending)
                if ok:
                    restart_mqtt()
                    return
                portal.error = (f"Verbindung zu „{pending[0]}“ fehlgeschlagen. "
                                "Bitte Passwort prüfen.")
                portal.networks = net.scan() or portal.networks
                net.hotspot_up()
                last_retry = time.time()

            if not net.hotspot_active():
                net.hotspot_up()

            if (net.saved_wifi() and time.time() - last_retry > RETRY_INTERVAL
                    and not net.clients_connected()):
                log.info("Niemand im Hotspot – versuche gespeichertes WLAN erneut")
                net.hotspot_down()
                if net.wait_online(CONNECT_TIMEOUT):
                    return
                portal.networks = net.scan() or portal.networks
                net.hotspot_up()
                last_retry = time.time()
            time.sleep(2)
    finally:
        portal.ap = False


def service():
    conf = load_conf(CONF)
    net = Net(conf)
    portal = Portal(conf)

    def stop(*_):
        portal.stop()
        net.hotspot_down()
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)

    net.hotspot_down()  # Reste eines früheren Laufs
    portal.start()      # Einstellungsseite; im Hotspot-Modus zusätzlich der Assistent
    log.info("Warte bis zu %ds auf Netzwerk …", BOOT_GRACE)
    offline_since = None if net.wait_online(BOOT_GRACE) else time.time() - OFFLINE_GRACE
    while True:
        if net.online():
            offline_since = None
            time.sleep(15)
            continue
        offline_since = offline_since or time.time()
        if time.time() - offline_since < OFFLINE_GRACE:
            time.sleep(10)
            continue
        log.info("Kein Netzwerk – starte Einrichtungsmodus")
        ap_mode(net, portal)
        offline_since = None


def main():
    ap = argparse.ArgumentParser(description="PiCam WLAN-Einrichtung")
    ap.add_argument("command", choices=["run", "status", "reset", "portal"],
                    nargs="?", default="run")
    args = ap.parse_args()
    logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"), format="%(name)s: %(message)s")
    conf = load_conf(CONF)
    net = Net(conf)

    if args.command == "run":
        service()
    elif args.command == "portal":
        # Hotspot + Portal sofort starten, unabhängig von eth0/WLAN (zum Testen oder
        # für manuelles Setup). Läuft bis Strg-C oder bis eine WLAN-Verbindung klappt.
        # Der Dienst belegt denselben Port – solange pausieren.
        run(["systemctl", "stop", "picam-wifi.service"], timeout=30)
        portal = Portal(conf)

        def stop(*_):
            portal.stop()
            net.hotspot_down()
            run(["systemctl", "start", "picam-wifi.service"], timeout=30)
            sys.exit(0)
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        log.info("Starte Hotspot '%s' + Portal auf Anforderung …", net.ap_ssid)
        portal.start()
        ap_mode(net, portal)
        log.info("Portal beendet.")
        stop()
    elif args.command == "status":
        print("Online:", net.online())
        print("Hotspot aktiv:", net.hotspot_active())
        print("Gespeicherte WLANs:", ", ".join(net.saved_wifi()) or "–")
    elif args.command == "reset":
        for name in net.saved_wifi():
            nmcli("con", "delete", name)
            print("gelöscht:", name)
        run(["systemctl", "restart", "picam-wifi.service"])
        print("Einrichtungsmodus startet (WLAN-Verbindung wird getrennt).")


if __name__ == "__main__":
    main()
