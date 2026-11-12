"""Gemeinsame Hilfsfunktionen für die OnviPi-Dienste."""
import json
import os
import re
import shlex
import socket
import subprocess
import uuid

CONF_DIR = os.environ.get("ONVIPI_CONF_DIR", "/etc/onvipi")
CONF = os.path.join(CONF_DIR, "onvipi.conf")
MQTT_CONF = os.path.join(CONF_DIR, "mqtt.conf")
GO2RTC_YAML = os.path.join(CONF_DIR, "go2rtc.yaml")
CAMERAS = os.path.join(CONF_DIR, "cameras.json")   # eine Kamera je Eintrag (onvipi_camera)
STREAM_NAME = "onvipi"


def load_conf(path=CONF):
    """Liest eine KEY='wert' Datei (bash-kompatibel)."""
    data = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                parts = shlex.split(value) if value.strip() else [""]
                data[key.strip()] = parts[0] if parts else ""
    except FileNotFoundError:
        pass
    return data


def save_conf(path, data, mode=0o640):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# managed by OnviPi – manual changes are kept\n")
        for key, value in data.items():
            f.write(f"{key}={shlex.quote(str(value))}\n")
    os.chmod(tmp, mode)
    set_group(tmp)
    os.replace(tmp, path)


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data, mode=0o640):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.chmod(tmp, mode)
    set_group(tmp)
    os.replace(tmp, path)


def set_group(path, group="onvipi"):
    """Dienste laufen als Benutzer 'onvipi' und brauchen Lesezugriff."""
    try:
        import shutil
        shutil.chown(path, group=group)
    except (LookupError, PermissionError, OSError):
        pass


def device_id():
    """Stabile ID dieses Geräts (aus /etc/machine-id)."""
    try:
        with open("/etc/machine-id") as f:
            mid = f.read().strip()
    except OSError:
        mid = socket.gethostname()
    # "picam:" ist der historische Namensraum (PiCam) – NICHT ändern, sonst ändert sich
    # die ONVIF-Geräte-ID und Home Assistant sieht ein neues Gerät.
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "picam:" + mid))


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "onvipi"


def primary_ip():
    """IP-Adresse der Standardroute (oder None)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # keine echten Pakete
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def primary_interface():
    """Name der Schnittstelle mit der Standardroute (oder None)."""
    ip = primary_ip()
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for m in re.finditer(r"^\d+:\s+(\S+)\s+inet (\d+\.\d+\.\d+\.\d+)/", out, re.M):
        if m.group(2) == ip:
            return m.group(1)
    return None


def interface_mac(iface):
    """MAC-Adresse einer Schnittstelle, z. B. 'dc:a6:32:01:02:03' (oder None)."""
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            mac = f.read().strip().lower()
    except (OSError, TypeError):
        return None
    return mac if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac) else None


def primary_mac():
    """MAC-Adresse der Standard-Schnittstelle (oder None)."""
    return interface_mac(primary_interface())


def builtin_mac():
    """MAC der fest verbauten Schnittstelle – ändert sich nicht mit dem aktiven Netz."""
    return interface_mac("eth0") or interface_mac("wlan0")


def ipv4_addresses():
    """Alle IPv4-Adressen außer Loopback."""
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    addrs = re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", out)
    return [a for a in addrs if not a.startswith("127.")]
