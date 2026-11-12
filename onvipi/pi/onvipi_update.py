#!/usr/bin/env python3
"""Update auf die neueste Version von GitHub ("System → Update" in der Oberfläche).

  onvipi_update.py check     neueste Version abfragen (JSON auf stdout)
  onvipi_update.py install   Paket laden, Prüfsumme prüfen, install.sh ausführen

"install" läuft als eigene systemd-Einheit (systemd-run), weil install.sh auch den
Dienst der Oberfläche neu startet. Der Fortschritt steht in STATUS (JSON).
Es wird nur dann eine Verbindung ins Internet aufgebaut, wenn der Nutzer in der
Oberfläche nach Updates sucht oder eines installiert.
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
    shutil.rmtree(WORK, ignore_errors=True)


def main(argv):
    if argv == ["check"]:
        print(json.dumps(check()))
        return 0
    if argv == ["install"]:
        try:
            install()
            return 0
        except Exception as ex:   # jede Ursache soll in der Oberfläche ankommen
            print(f"Update failed: {ex}", file=sys.stderr)
            write_status("failed", str(ex))
            return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
