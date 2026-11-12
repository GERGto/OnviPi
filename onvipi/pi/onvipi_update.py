#!/usr/bin/env python3
"""Updates von GitHub (Seite "Updates" der Oberfläche).

  onvipi_update.py check     neueste Version abfragen (JSON auf stdout)
  onvipi_update.py install   Paket laden, Prüfsumme prüfen, install.sh ausführen
  onvipi_update.py auto      nächtlicher Lauf (onvipi-autoupdate.timer): prüfen und –
                             wenn UPDATE_AUTO nicht '0' ist – gleich installieren

Automatische Updates sind standardmäßig an (UPDATE_AUTO fehlt oder '1'). Abschalten auf
der Seite "Updates"; dann wird auch nicht mehr automatisch nach Updates gesucht und es
gibt keine Verbindung ins Internet, solange der Nutzer nicht selbst sucht.

"install" aus der Oberfläche läuft als eigene systemd-Einheit (systemd-run), weil
install.sh auch den Dienst der Oberfläche neu startet. Fortschritt: STATUS (flüchtig),
Verlauf (letzte Prüfung, letztes Update): STATE (bleibt über Neustarts erhalten).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from onvipilib import CONF, load_conf  # noqa: E402

REPO = "GERGto/OnviPi"
STATUS = os.environ.get("ONVIPI_UPDATE_STATUS", "/run/onvipi-update.json")
STATE = os.environ.get("ONVIPI_UPDATE_STATE", "/var/lib/onvipi/update-state.json")
WORK = "/var/tmp/onvipi-update"
PACKAGE = "onvipi.tar.gz"
UNIT = "onvipi-update"


def repo():
    name = load_conf(CONF).get("UPDATE_REPO", REPO)
    return name if re.fullmatch(r"[\w.-]+/[\w.-]+", name) else REPO


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "OnviPi-updater",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def current_version():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")) as f:
            return f.read().strip() or "dev"
    except OSError:
        return "dev"


def version_key(text):
    """'2.10' -> (2, 10); None, wenn es keine veröffentlichte Version ist (z. B. 'dev')."""
    if not re.fullmatch(r"\d+(\.\d+)*", text or ""):
        return None
    return tuple(int(p) for p in text.split("."))


def check():
    """{current, latest, available, url} – wirft OSError/ValueError ohne Internet."""
    data = json.loads(fetch(f"https://api.github.com/repos/{repo()}/releases/latest"))
    latest = str(data.get("tag_name", "")).lstrip("v")
    cur, new = version_key(current_version()), version_key(latest)
    return {"current": current_version(), "latest": latest,
            "available": bool(new) and (cur is None or new > cur),
            "url": data.get("html_url", "")}


def auto_enabled(conf=None):
    conf = load_conf(CONF) if conf is None else conf
    return conf.get("UPDATE_AUTO", "1") != "0"


def read_state():
    """Verlauf: {last_check, latest, available, url, last_update: {version, time, ok, error}}."""
    try:
        with open(STATE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(**changes):
    state = read_state()
    state.update(changes)
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, STATE)
    except OSError:
        pass
    return state


def check_and_store():
    """Wie check(), merkt sich aber das Ergebnis für die Oberfläche."""
    try:
        info = check()
    except (OSError, ValueError) as ex:
        save_state(last_check=time.time(), check_error=str(ex))
        raise
    save_state(last_check=time.time(), check_error="", latest=info["latest"],
               available=info["available"], url=info["url"])
    return info


def read_status():
    try:
        with open(STATUS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"phase": "idle", "error": ""}


def write_status(phase, error="", **extra):
    tmp = STATUS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dict(extra, phase=phase, error=error, updated=time.time()), f)
    os.replace(tmp, STATUS)


def start():
    """Startet die Installation losgelöst vom Dienst der Oberfläche. Fehlermeldung oder None."""
    if read_status().get("phase") in ("downloading", "installing") and \
            time.time() - read_status().get("updated", 0) < 1800:
        return "An update is already running."
    write_status("downloading")
    try:
        r = subprocess.run(["systemd-run", "--unit", UNIT, "--collect", "--no-block",
                            "/usr/bin/python3", os.path.abspath(__file__), "install"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as ex:
        write_status("failed", str(ex))
        return f"Could not start the update: {ex}"
    if r.returncode != 0:
        write_status("failed", r.stderr.strip())
        return "Could not start the update – see the log."
    return None


def install():
    base = f"https://github.com/{repo()}/releases/latest/download"
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, mode=0o700)
    write_status("downloading")
    package = fetch(f"{base}/{PACKAGE}", timeout=120)
    sums = fetch(f"{base}/SHA256SUMS").decode("utf-8", "replace")
    want = next((line.split()[0] for line in sums.splitlines()
                 if line.split()[1:] and line.split()[1].lstrip("*") == PACKAGE), None)
    if not want or hashlib.sha256(package).hexdigest() != want.lower():
        raise RuntimeError("The checksum of the download does not match.")
    path = os.path.join(WORK, PACKAGE)
    with open(path, "wb") as f:
        f.write(package)
    with tarfile.open(path) as tar:
        try:
            tar.extractall(WORK, filter="data")
        except TypeError:   # Python < 3.12 (Bookworm): Pfade selbst prüfen
            for member in tar.getmembers():
                target = os.path.realpath(os.path.join(WORK, member.name))
                if not target.startswith(os.path.realpath(WORK) + os.sep) or member.islnk() \
                        or member.issym():
                    raise RuntimeError("Unexpected content in the package.")
            tar.extractall(WORK)
    script = os.path.join(WORK, "onvipi", "install.sh")
    version = "?"
    try:
        with open(os.path.join(WORK, "onvipi", "VERSION")) as f:
            version = f.read().strip()
    except OSError:
        pass
    write_status("installing", version=version)
    r = subprocess.run(["bash", script], timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"The installer failed (exit code {r.returncode}) – see the log.")
    write_status("done", version=version)
    save_state(available=False, last_update={"version": version, "time": time.time(),
                                             "ok": True, "error": ""})
    shutil.rmtree(WORK, ignore_errors=True)


def auto():
    """Nächtlicher Lauf. Gibt 0 zurück, auch wenn nichts zu tun war."""
    if not auto_enabled():
        print("Automatic updates are switched off.")
        return 0
    if version_key(current_version()) is None:
        print("Development build – not updated automatically.")
        return 0
    info = check_and_store()
    if not info["available"]:
        print(f"Up to date ({info['current']}).")
        return 0
    print(f"Installing version {info['latest']} (installed: {info['current']}) …")
    install()
    return 0


def main(argv):
    if argv == ["check"]:
        print(json.dumps(check()))
        return 0
    if argv in (["install"], ["auto"]):
        try:
            return auto() if argv == ["auto"] else install() or 0
        except Exception as ex:   # jede Ursache soll in der Oberfläche ankommen
            print(f"Update failed: {ex}", file=sys.stderr)
            if read_status().get("phase") in ("downloading", "installing"):
                write_status("failed", str(ex))
                save_state(last_update={"version": read_state().get("latest", "?"),
                                        "time": time.time(), "ok": False, "error": str(ex)})
            return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
