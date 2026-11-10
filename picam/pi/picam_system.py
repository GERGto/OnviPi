#!/usr/bin/env python3
"""Systemeinstellungen für den Einrichtungs-Assistenten (läuft als root in picam-wifi).

Benutzerkonto + Passwort, SSH, WLAN-Land, Zeitzone und Hostname. Damit lässt sich ein
frisch geflashtes PiCam-Image komplett über den Hotspot einrichten – ohne Einstellungen
im Raspberry Pi Imager und ohne Tastatur/Bildschirm.

Sicherheitsregel: Ein Benutzerkonto kann der (offene) Assistent nur anlegen, solange es
noch keinen Benutzer mit Passwort gibt. Danach ist dieser Teil gesperrt – sonst könnte
jeder in Reichweite des Hotspots (z. B. bei einem WLAN-Ausfall) das Passwort neu setzen.
"""
import os
import re
import socket
import subprocess
import unicodedata

USERCONF = "/usr/lib/userconf-pi/userconf"   # offizielles Werkzeug von Raspberry Pi OS
USER_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
LOGIN_GROUPS = ("sudo", "adm", "video", "audio", "plugdev", "users", "netdev", "dialout",
                "gpio", "i2c", "spi")
FALLBACK_COUNTRIES = [("DE", "Deutschland"), ("AT", "Österreich"), ("CH", "Schweiz"),
                      ("LU", "Luxemburg"), ("NL", "Niederlande"), ("BE", "Belgien"),
                      ("FR", "Frankreich"), ("IT", "Italien"), ("PL", "Polen"),
                      ("GB", "Vereinigtes Königreich"), ("US", "USA")]


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
            return f"Benutzer konnte nicht angelegt werden: {r.stderr.strip()}"
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
        return "Es ist bereits ein Benutzer eingerichtet."
    if not USER_RE.fullmatch(name):
        return ("Benutzername: Kleinbuchstaben, Ziffern, - und _, beginnt mit einem Buchstaben "
                "(max. 32 Zeichen).")
    if not 8 <= len(password) <= 128 or any(ord(c) < 32 for c in password):
        return "Das Passwort muss 8 bis 128 Zeichen lang sein."
    uids = {p[0]: int(p[2]) for p in _passwd() if p[2].isdigit()}
    if name in uids and not 1000 <= uids[name] < 60000:
        return f"„{name}“ ist ein Systemkonto – bitte einen anderen Namen wählen."
    first = next((u for u, uid in uids.items() if uid == 1000), None)
    if first and first != name:
        if name in uids:
            return f"Der Name „{name}“ ist schon vergeben."
        err = _rename_user(first, name, password)
        if err:
            return err
    elif name not in uids:
        r = _run(["useradd", "-m", "-s", "/bin/bash", name])
        if r.returncode != 0:
            return f"Benutzer konnte nicht angelegt werden: {r.stderr.strip()}"
    groups = [g for g in LOGIN_GROUPS if _run(["getent", "group", g]).returncode == 0]
    if groups:
        _run(["usermod", "-aG", ",".join(groups), name])
    r = _run(["chpasswd"], input=f"{name}:{password}\n")
    if r.returncode != 0:
        return f"Passwort konnte nicht gesetzt werden: {r.stderr.strip()}"
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
    """'Garten Süd' -> 'picam-garten-sud'. None beim Standardnamen (Hostname bleibt dann)."""
    s = unicodedata.normalize("NFKD", name.replace("ß", "ss")).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    if not s or s == "picam":
        return None
    return ("picam-" + s)[:63].rstrip("-")


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
