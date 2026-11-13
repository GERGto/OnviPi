#!/usr/bin/env python3
"""Systemeinstellungen für Assistent und Weboberfläche (läuft als root in onvipi-wifi).

Benutzerkonto + Passwort, SSH, WLAN-Land, Zeitzone, Hostname, Kennzahlen und Terminal. Damit lässt sich ein
frisch geflashtes OnviPi-Image komplett über den Hotspot einrichten – ohne Einstellungen
im Raspberry Pi Imager und ohne Tastatur/Bildschirm.

Sicherheitsregel: Ein Benutzerkonto kann der (offene) Assistent nur anlegen, solange es
noch keinen Benutzer mit Passwort gibt. Danach ist dieser Teil gesperrt – sonst könnte
jeder in Reichweite des Hotspots (z. B. bei einem WLAN-Ausfall) das Passwort neu setzen.
"""
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from onvipilib import (CAMERAS, CONF, GO2RTC_YAML, MQTT_CONF, load_conf, load_json,  # noqa: E402
                       save_conf)

USERCONF = "/usr/lib/userconf-pi/userconf"   # offizielles Werkzeug von Raspberry Pi OS
USER_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
LOGIN_GROUPS = ("sudo", "adm", "video", "audio", "plugdev", "users", "netdev", "dialout",
                "gpio", "i2c", "spi")
FALLBACK_COUNTRIES = [("DE", "Germany"), ("AT", "Austria"), ("CH", "Switzerland"),
                      ("LU", "Luxembourg"), ("NL", "Netherlands"), ("BE", "Belgium"),
                      ("FR", "France"), ("IT", "Italy"), ("PL", "Poland"),
                      ("GB", "United Kingdom"), ("US", "United States")]


def _run(cmd, input=None, timeout=60):
    try:
        return subprocess.run(cmd, input=input, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as ex:
        return subprocess.CompletedProcess(cmd, 127, "", str(ex))


# --------------------------------------------------------------------------- Benutzer
def _passwd():
    with open("/etc/passwd", encoding="utf-8") as f:
        return [p for p in (line.rstrip("\n").split(":") for line in f) if len(p) >= 7]


def login_users():
    """Normale Benutzerkonten (UID 1000–59999)."""
    return [p[0] for p in _passwd() if p[2].isdigit() and 1000 <= int(p[2]) < 60000]


def users_with_password():
    """Benutzerkonten mit gesetztem (nicht gesperrtem) Passwort, None wenn nicht lesbar."""
    try:
        with open("/etc/shadow", encoding="utf-8") as f:
            shadow = {p[0]: p[1] for p in (line.split(":") for line in f) if len(p) > 1}
    except OSError:
        return None
    return [u for u in login_users() if shadow.get(u) and shadow[u][0] not in "!*"]


def users_locked():
    """Anmeldekonten ohne nutzbares Passwort (z. B. nach dem Zurücksetzen)."""
    with_pw = users_with_password()
    return None if with_pw is None else [u for u in login_users() if u not in with_pw]


def configured():
    """True, sobald ein Benutzer mit Passwort existiert (oder es sich nicht prüfen lässt)."""
    users = users_with_password()
    return users is None or bool(users)


def _hash_password(password):
    r = _run(["openssl", "passwd", "-6", "-stdin"], input=password + "\n")
    return r.stdout.strip() if r.returncode == 0 and r.stdout.startswith("$") else None


def _rename_user(old, new, password):
    """Benennt den vorhandenen Erstbenutzer (UID 1000, im Image 'pi') um."""
    pw_hash = _hash_password(password)
    if os.path.exists(USERCONF) and pw_hash:
        r = _run([USERCONF, new, pw_hash], timeout=120)
        if r.returncode == 0:
            return None
    # Ersatzweg – entspricht dem, was der Raspberry Pi Imager ohne userconf macht
    for cmd in (["usermod", "-l", new, old], ["usermod", "-m", "-d", f"/home/{new}", new],
                ["groupmod", "-n", new, old]):
        r = _run(cmd)
        if r.returncode != 0 and cmd[0] == "usermod":
            return f"Could not create the user: {r.stderr.strip()}"
    sudoers = "/etc/sudoers.d/010_pi-nopasswd"
    if os.path.exists(sudoers):
        with open(sudoers, encoding="utf-8") as f:
            text = f.read()
        with open(sudoers, "w", encoding="utf-8") as f:
            f.write(re.sub(rf"^{re.escape(old)} ", f"{new} ", text, flags=re.M))
    return None


def create_login(name, password):
    """Legt das Anmeldekonto an (bzw. benennt 'pi' um). Gibt eine Fehlermeldung oder None zurück."""
    if configured():
        return "A user has already been set up."
    if not USER_RE.fullmatch(name):
        return ("Username: lowercase letters, digits, - and _, starting with a letter "
                "(max. 32 characters).")
    if not 8 <= len(password) <= 128 or any(ord(c) < 32 for c in password):
        return "The password must be 8 to 128 characters long."
    if name in (users_locked() or []):
        # Passwort wurde über die Reset-Datei gesperrt -> nur neu setzen
        r = _run(["chpasswd"], input=f"{name}:{password}\n")
        return None if r.returncode == 0 else f"Could not set the password: {r.stderr.strip()}"
    uids = {p[0]: int(p[2]) for p in _passwd() if p[2].isdigit()}
    if name in uids and not 1000 <= uids[name] < 60000:
        return f"“{name}” is a system account – please choose another name."
    first = next((u for u, uid in uids.items() if uid == 1000), None)
    if first and first != name:
        if name in uids:
            return f"The name “{name}” is already taken."
        err = _rename_user(first, name, password)
        if err:
            return err
    elif name not in uids:
        r = _run(["useradd", "-m", "-s", "/bin/bash", name])
        if r.returncode != 0:
            return f"Could not create the user: {r.stderr.strip()}"
    groups = [g for g in LOGIN_GROUPS if _run(["getent", "group", g]).returncode == 0]
    if groups:
        _run(["usermod", "-aG", ",".join(groups), name])
    r = _run(["chpasswd"], input=f"{name}:{password}\n")
    if r.returncode != 0:
        return f"Could not set the password: {r.stderr.strip()}"
    return None


# --------------------------------------------------------------------------- SSH
def ssh_enabled():
    for unit in ("ssh.service", "ssh.socket"):
        if _run(["systemctl", "is-enabled", unit]).stdout.strip() == "enabled":
            return True
    return False


def set_ssh(enable):
    if enable:
        if not any(f.startswith("ssh_host_") for f in os.listdir("/etc/ssh")):
            _run(["ssh-keygen", "-A"])
        return _run(["systemctl", "enable", "--now", "ssh.service"]).returncode == 0
    _run(["systemctl", "disable", "--now", "ssh.socket"])
    return _run(["systemctl", "disable", "--now", "ssh.service"]).returncode == 0


# --------------------------------------------------------------------------- Land, Zeitzone
def countries():
    """[(Code, Name)] aus tzdata, nach Name sortiert; DE/AT/CH zuerst."""
    try:
        with open("/usr/share/zoneinfo/iso3166.tab", encoding="utf-8") as f:
            items = [tuple(line.rstrip("\n").split("\t", 1)) for line in f
                     if line.strip() and not line.startswith("#") and "\t" in line]
    except OSError:
        return FALLBACK_COUNTRIES
    items.sort(key=lambda c: c[1])
    top = [c for code in ("DE", "AT", "CH") for c in items if c[0] == code]
    return top + [c for c in items if c not in top]


def set_country(code):
    if os.path.exists("/usr/bin/raspi-config"):
        _run(["raspi-config", "nonint", "do_wifi_country", code], timeout=60)
    else:
        _run(["iw", "reg", "set", code])
    _run(["rfkill", "unblock", "wifi"])


def timezones():
    zones = _run(["timedatectl", "list-timezones"]).stdout.split()
    return zones or ["Europe/Berlin", "Europe/Vienna", "Europe/Zurich", "UTC"]


def current_timezone():
    tz = _run(["timedatectl", "show", "-p", "Timezone", "--value"]).stdout.strip()
    if not tz:
        tz = os.path.realpath("/etc/localtime").partition("/zoneinfo/")[2]
    return tz or "UTC"


def set_timezone(tz):
    return _run(["timedatectl", "set-timezone", tz]).returncode == 0


# --------------------------------------------------------------------------- Hostname
def hostname_for(name):
    """'Garten Süd' -> 'onvipi-garten-sud'. None beim Standardnamen (Hostname bleibt dann)."""
    s = unicodedata.normalize("NFKD", name.replace("ß", "ss")).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    if not s or s == "onvipi":
        return None
    return ("onvipi-" + s)[:63].rstrip("-")


def set_hostname(new):
    old = socket.gethostname()
    if not new or new == old:
        return
    if _run(["hostnamectl", "set-hostname", new]).returncode != 0:
        with open("/etc/hostname", "w", encoding="utf-8") as f:
            f.write(new + "\n")
        _run(["hostname", new])
    with open("/etc/hosts", encoding="utf-8") as f:
        hosts = f.read()
    if re.search(r"^127\.0\.1\.1\s", hosts, re.M):
        hosts = re.sub(r"^127\.0\.1\.1\s.*$", f"127.0.1.1\t{new}", hosts, flags=re.M)
    else:
        hosts += f"127.0.1.1\t{new}\n"
    with open("/etc/hosts", "w", encoding="utf-8") as f:
        f.write(hosts)
    _run(["systemctl", "try-restart", "avahi-daemon.service"])


# --------------------------------------------------------------------------- Ein Konto
# Es gibt genau EIN Konto: Benutzername + Passwort gelten für die Weboberfläche, für
# ONVIF/RTSP (Home Assistant) und für die Anmeldung am Pi (Terminal, SSH).
# Das Passwort landet in der RTSP-Adresse, deshalb der eingeschränkte Zeichensatz.
PASSWORD_RE = re.compile(r"[A-Za-z0-9._~!*+=-]{8,64}")
PASSWORD_HINT = "8–64 characters: letters, digits and . _ ~ ! * + = -"
RESET_MARKERS = ("/boot/firmware/onvipi-reset", "/boot/firmware/onvipi-reset.txt",
                 "/boot/onvipi-reset", "/boot/onvipi-reset.txt")


def default_username(conf):
    """Vorschlag für den Assistenten: bisheriger Name, sonst 'admin'."""
    if conf.get("LOGIN_USER"):
        return conf["LOGIN_USER"]
    try:
        first = next((p[0] for p in _passwd() if p[2] == "1000"), None)
    except OSError:
        first = None
    return first if first and first != "pi" else "admin"


def check_account(name, password):
    """Prüft Name und Passwort, ohne etwas zu ändern. Fehlermeldung oder None."""
    if not USER_RE.fullmatch(name or ""):
        return ("Username: lowercase letters, digits, - and _, starting with a letter "
                "(max. 32 characters).")
    if not PASSWORD_RE.fullmatch(password or ""):
        return "Password: " + PASSWORD_HINT
    try:
        uids = {p[0]: int(p[2]) for p in _passwd() if p[2].isdigit()}
    except OSError:
        uids = {}
    if name in uids and not 1000 <= uids[name] < 60000:
        return f"“{name}” is a system account – please choose another name."
    return None


def set_login_password(name, password):
    """Setzt das Passwort eines bestehenden Anmeldekontos."""
    if name not in login_users():
        return False
    return _run(["chpasswd"], input=f"{name}:{password}\n").returncode == 0


def consume_reset_marker():
    """Passwort vergessen: Datei 'onvipi-reset' auf der Boot-Partition der SD-Karte anlegen.

    Beim nächsten Start werden die Passwörter der Anmeldekonten gesperrt – der
    Einrichtungsassistent erscheint wieder und vergibt ein neues. Kameras und Netzwerk
    bleiben erhalten. Gibt True zurück, wenn zurückgesetzt wurde.
    """
    found = [m for m in RESET_MARKERS if os.path.exists(m)]
    if not found:
        return False
    for user in users_with_password() or []:
        _run(["passwd", "-l", user])
    for marker in found:
        try:
            os.remove(marker)
        except OSError:
            pass
    return True


# --------------------------------------------------------------------------- Zustand
_cpu_last = None


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def cpu_percent():
    """CPU-Auslastung seit dem letzten Aufruf (alle Kerne zusammen, 0–100)."""
    global _cpu_last
    parts = _read("/proc/stat").split("\n", 1)[0].split()[1:]
    if len(parts) < 4:
        return None
    nums = [int(x) for x in parts]
    idle, total = nums[3] + (nums[4] if len(nums) > 4 else 0), sum(nums)
    last, _cpu_last = _cpu_last, (idle, total)
    if not last or total == last[1]:
        return None
    return round(100 * (1 - (idle - last[0]) / (total - last[1])))


def stats():
    """Kennzahlen für die Übersicht. Fehlende Werte sind None."""
    temp = _read("/sys/class/thermal/thermal_zone0/temp").strip()
    mem = dict(re.findall(r"^(\w+):\s+(\d+)", _read("/proc/meminfo"), re.M))
    up = _read("/proc/uptime").split()
    throttled = None
    m = re.search(r"0x([0-9a-fA-F]+)", _run(["vcgencmd", "get_throttled"], timeout=5).stdout)
    if m:
        throttled = int(m.group(1), 16)
    try:
        disk = shutil.disk_usage("/")
        disk_percent = round(100 * disk.used / disk.total)
    except OSError:
        disk_percent = None
    return {
        "cpu_temp": round(int(temp) / 1000, 1) if temp.isdigit() else None,
        "cpu_percent": cpu_percent(),
        "cores": os.cpu_count() or 4,
        "mem_percent": (round(100 * (1 - int(mem["MemAvailable"]) / int(mem["MemTotal"])))
                        if mem.get("MemTotal") and mem.get("MemAvailable") else None),
        "disk_percent": disk_percent,
        "uptime": int(float(up[0])) if up else None,
        "undervoltage": bool(throttled & 0x1) if throttled is not None else None,
        "undervoltage_seen": bool(throttled & 0x10000) if throttled is not None else None,
        "throttled": bool(throttled & 0x4) if throttled is not None else None,
    }


def version():
    here = os.path.dirname(os.path.abspath(__file__))
    return _read(os.path.join(here, "VERSION")).strip() or "dev"


def logs(lines=800):
    """Protokoll aller OnviPi-Dienste als Text (für den Download in der Oberfläche)."""
    r = _run(["journalctl", "--no-pager", "-n", str(lines), "-o", "short-iso",
              "-u", "onvipi-*"], timeout=30)
    return r.stdout or r.stderr or "No log available.\n"


def reboot():
    subprocess.Popen(["systemctl", "reboot"])


def ssh_fingerprints():
    """Fingerabdrücke der SSH-Schlüssel dieses Geräts, z. B. ['ED25519 SHA256:kB/jc…']."""
    out = []
    for kind in ("ed25519", "ecdsa", "rsa"):
        r = _run(["ssh-keygen", "-lf", f"/etc/ssh/ssh_host_{kind}_key.pub"], timeout=5)
        parts = r.stdout.split()
        if r.returncode == 0 and len(parts) >= 2:
            out.append(f"{parts[-1].strip('()')} {parts[1]}")
    return out


# --------------------------------------------------------------------------- Terminal
class Terminal:
    """Web-Terminal (ttyd) – standardmäßig aus, nur zeitlich begrenzt einschaltbar.

    ttyd lauscht nur auf 127.0.0.1; die Weboberfläche reicht es angemeldeten Benutzern
    unter /terminal/ durch. Nach Ablauf der Frist und nach jedem Neustart ist es wieder aus.

    Wer die Weboberfläche nutzt, hat sich schon mit dem OnviPi-Konto angemeldet – das
    Terminal öffnet deshalb direkt eine Shell dieses Kontos (runuser -l). Nicht `login`:
    das trennt beim Start kurz das Terminal (vhangup), woran ttyd hängen bleibt – man sieht
    nur einen Cursor. `login` bleibt nur als Notlösung, wenn es kein OnviPi-Konto gibt
    (Paket-Installation); dann meldet man sich mit einem Benutzer des Systems an.
    """
    PORT = 7681
    BASE = "/terminal"

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.until = 0
        self.timer = None

    @staticmethod
    def available():
        return bool(shutil.which("ttyd"))

    @staticmethod
    def shell_command(user):
        """Befehl, den ttyd für jede Verbindung startet."""
        if user and user in login_users():
            tool = shutil.which("runuser") or shutil.which("su") or "su"
            return [tool, "-l", user]
        return ["login"]

    def start(self, minutes, user=None):
        minutes = max(5, min(240, int(minutes)))
        self.stop()
        cmd = ["ttyd", "-i", "lo", "-p", str(self.PORT), "-b", self.BASE,
               "-t", "titleFixed=OnviPi terminal", "-t", "disableLeaveAlert=true"]
        usage = _run(["ttyd", "--help"], timeout=5)
        if "--writable" in usage.stdout + usage.stderr:
            cmd.append("-W")   # ab ttyd 1.7.4 ist das Terminal sonst nur lesbar
        cmd += self.shell_command(user)
        with self.lock:
            try:
                # Ausgaben von ttyd ins Protokoll von onvipi-wifi (journalctl -u onvipi-wifi)
                self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
            except OSError as ex:
                return f"Could not start the terminal: {ex}"
            self.until = time.time() + minutes * 60
            self.timer = threading.Timer(minutes * 60, self.stop)
            self.timer.daemon = True
            self.timer.start()
        return None

    def stop(self):
        with self.lock:
            proc, self.proc, self.until = self.proc, None, 0
            if self.timer:
                self.timer.cancel()
                self.timer = None
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def active(self):
        with self.lock:
            return bool(self.proc and self.proc.poll() is None and time.time() < self.until)

    def state(self):
        active = self.active()
        return {"available": self.available(), "active": active,
                "seconds_left": max(0, int(self.until - time.time())) if active else 0}


# --------------------------------------------------------------------------- Sicherung
NM_DIR = "/etc/NetworkManager/system-connections"
BACKUP_FORMAT = 1
NO_RESTORE = ("LOGIN_USER",)   # gehört zum Gerät, nicht zur Sicherung


def _wifi_profiles():
    """Gespeicherte WLAN-Profile (ohne Hotspot) als {dateiname: inhalt}."""
    out = {}
    try:
        names = sorted(os.listdir(NM_DIR))
    except OSError:
        return out
    for name in names:
        text = _read(os.path.join(NM_DIR, name))
        if name.endswith(".nmconnection") and "type=wifi" in text and "mode=ap" not in text:
            out[name] = text
    return out


def backup():
    """Alle Einstellungen als ein JSON-Dokument. Enthält Passwörter (Konto, WLAN, MQTT)."""
    return {"format": BACKUP_FORMAT, "product": "OnviPi", "version": version(),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "hostname": socket.gethostname(),
            "timezone": current_timezone(), "conf": load_conf(CONF),
            "cameras": (load_json(CAMERAS) or {}).get("cameras", []),
            "mqtt": load_conf(MQTT_CONF), "wifi": _wifi_profiles()}


def restore(data):
    """Spielt eine Sicherung ein. Gibt eine Fehlermeldung oder None zurück."""
    import onvipi_camera as camera   # hier, weil onvipi_camera beim Start viel einliest
    if not isinstance(data, dict) or data.get("product") != "OnviPi" \
            or data.get("format") != BACKUP_FORMAT or not isinstance(data.get("conf"), dict):
        return "This file is not an OnviPi backup."
    new = {str(k): str(v) for k, v in data["conf"].items()
           if re.fullmatch(r"[A-Z][A-Z0-9_]*", str(k)) and k not in NO_RESTORE}
    if not PASSWORD_RE.fullmatch(new.get("CAM_PASSWORD", "")):
        return "The backup contains no valid password."
    conf = load_conf(CONF)
    login = conf.get("LOGIN_USER", "")
    conf.update(new)
    if login:
        conf["LOGIN_USER"] = login
        set_login_password(login, conf["CAM_PASSWORD"])
    save_conf(CONF, conf)
    cams = [c for c in data.get("cameras") or [] if isinstance(c, dict)]
    if cams:
        camera.save_cameras(cams)
    if isinstance(data.get("mqtt"), dict):
        save_conf(MQTT_CONF, {str(k): str(v) for k, v in data["mqtt"].items()}, mode=0o600)
    for name, text in (data.get("wifi") or {}).items():
        name = os.path.basename(str(name))
        if not name.endswith(".nmconnection") or "[connection]" not in str(text):
            continue
        path = os.path.join(NM_DIR, name)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(str(text))
            os.chmod(path, 0o600)
        except OSError:
            pass
    _run(["nmcli", "con", "reload"])
    if data.get("timezone") in timezones():
        set_timezone(data["timezone"])
    host = str(data.get("hostname") or "")
    if re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", host):
        set_hostname(host)
    if conf.get("WIFI_COUNTRY"):
        set_country(conf["WIFI_COUNTRY"])
    return None


def factory_reset():
    """Setzt OnviPi auf den Auslieferungszustand zurück und startet neu.

    Kameras, Netzwerk (WLANs), MQTT und Name werden gelöscht. Auf OnviPiOS wird auch das
    Konto entfernt – der Einrichtungsassistent erscheint wieder. Bei der Paket-Installation
    gehört das Linux-Konto dem Nutzer und bleibt, ebenso das bisherige Passwort.
    Aufnahmen auf dem USB-Speicher bleiben erhalten.
    """
    old = load_conf(CONF)
    login = old.get("LOGIN_USER", "")
    _run(["systemctl", "stop", "onvipi-record.service"], timeout=60)
    for path in (CAMERAS, MQTT_CONF, GO2RTC_YAML, CONF):
        try:
            os.remove(path)
        except OSError:
            pass
    try:   # mit dem Standardnamen vergibt firstboot.sh wieder "onvipi-<id>"
        with open("/etc/hostname", "w", encoding="utf-8") as f:
            f.write("raspberrypi\n")
    except OSError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    env = dict(os.environ, WIFI_COUNTRY=old.get("WIFI_COUNTRY", "DE"))
    try:
        subprocess.run(["bash", os.path.join(here, "setup", "firstboot.sh")], env=env,
                       timeout=300, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass
    if login:
        _run(["passwd", "-l", login])
    else:
        conf = load_conf(CONF)
        conf.update({k: old[k] for k in ("CAM_USER", "CAM_PASSWORD") if k in old})
        save_conf(CONF, conf)
    set_ssh(False)
    out = _run(["nmcli", "-t", "-f", "NAME,TYPE", "con", "show"]).stdout
    for line in out.splitlines():
        name, _, kind = line.rpartition(":")
        if kind in ("802-11-wireless", "wifi"):
            _run(["nmcli", "con", "delete", name.replace("\\:", ":")])
    reboot()
