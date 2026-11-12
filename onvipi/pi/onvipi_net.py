#!/usr/bin/env python3
"""Netzwerk über NetworkManager (nmcli): Schnittstellen, WLAN, Hotspot, IP-Einstellungen.

* Mehrere WLAN-Geräte (eingebaut + USB-Adapter): WIFI_IFACE in onvipi.conf wählt das
  Gerät für das Heimnetz. Der Einrichtungs-Hotspot läuft bevorzugt auf dem eingebauten
  WLAN – ist das Heimnetz-Gerät ein anderes, bleibt der Hotspot beim Verbinden an.
* IP-Einstellungen (DHCP / feste Adresse) werden mit Rücknahme-Frist gesetzt: Wer sich
  dabei aussperrt, bekommt nach ROLLBACK_SECONDS automatisch die alten Werte zurück.
"""
import ipaddress
import logging
import os
import re
import subprocess
import threading
import time

log = logging.getLogger("onvipi-net")

HOTSPOT = "onvipi-hotspot"
AP_IP = "10.42.0.1"
AP_NET = "10.42.0."
CONNECT_TIMEOUT = 45
ROLLBACK_SECONDS = 180
WIFI_TYPES = ("wifi", "802-11-wireless")
ETH_TYPES = ("ethernet", "802-3-ethernet")


def run(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")
    except OSError as ex:
        return subprocess.CompletedProcess(cmd, 127, "", str(ex))


def nmcli(*args, timeout=30):
    r = run(["nmcli", *args], timeout=timeout)
    if r.returncode != 0:
        log.debug("nmcli %s -> %s %s", " ".join(args), r.returncode, r.stderr.strip())
    return r


def split_terse(line):
    """nmcli -t trennt mit ':' und maskiert ':' in Werten als '\\:'."""
    return [p.replace("\\:", ":").replace("\\\\", "\\") for p in re.split(r"(?<!\\):", line)]


def terse_props(text):
    """'KEY:wert'-Zeilen von `nmcli -t … show` -> {KEY: [werte]} (IP4.ADDRESS[1] -> IP4.ADDRESS)."""
    props = {}
    for line in text.splitlines():
        parts = split_terse(line)
        if len(parts) < 2:
            continue
        key = re.sub(r"\[\d+\]$", "", parts[0])
        value = ":".join(parts[1:]).strip()
        if value and value != "--":
            props.setdefault(key, []).append(value)
    return props


# --------------------------------------------------------------------------- Schnittstellen
def devices():
    """[{iface, type ('wifi'|'ethernet'), state, connection}] – ohne Loopback, Bridges usw."""
    out = nmcli("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev", "status").stdout
    items = []
    for f in map(split_terse, out.splitlines()):
        if len(f) < 4:
            continue
        kind = "wifi" if f[1] in WIFI_TYPES else "ethernet" if f[1] in ETH_TYPES else None
        if kind and not f[0].startswith("p2p-"):
            items.append({"iface": f[0], "type": kind, "state": f[2].split(" ")[0],
                          "connection": f[3] if f[3] not in ("", "--") else ""})
    return items


def is_usb(iface):
    return "/usb" in os.path.realpath(f"/sys/class/net/{iface}/device")


def device_label(iface, kind, props=None):
    if kind == "ethernet":
        return "USB Ethernet adapter" if is_usb(iface) else "Ethernet (cable)"
    if not is_usb(iface):
        return "Built-in Wi-Fi"
    props = props or {}
    product = " ".join((props.get("GENERAL.VENDOR", [""])[0] + " "
                        + props.get("GENERAL.PRODUCT", [""])[0]).split())
    return f"USB Wi-Fi adapter ({product})" if product else "USB Wi-Fi adapter"


def wifi_devices():
    """WLAN-Geräte für die Auswahl: [{iface, label, usb}], eingebautes zuerst."""
    items = []
    for d in devices():
        if d["type"] != "wifi":
            continue
        props = terse_props(nmcli("-t", "-f", "GENERAL.VENDOR,GENERAL.PRODUCT",
                                  "dev", "show", d["iface"]).stdout)
        items.append({"iface": d["iface"], "usb": is_usb(d["iface"]),
                      "label": device_label(d["iface"], "wifi", props)})
    return sorted(items, key=lambda d: (d["usb"], d["iface"]))


def ethernet_connected():
    return any(d["type"] == "ethernet" and d["state"] == "connected" for d in devices())


def interfaces():
    """Ausführlicher Zustand je Schnittstelle für die Oberfläche."""
    items = []
    for d in devices():
        props = terse_props(nmcli(
            "-t", "-f", "GENERAL.HWADDR,GENERAL.VENDOR,GENERAL.PRODUCT,IP4.ADDRESS,"
            "IP4.GATEWAY,IP4.DNS", "dev", "show", d["iface"]).stdout)
        addr = props.get("IP4.ADDRESS", [""])[0]
        item = dict(d, usb=is_usb(d["iface"]),
                    label=device_label(d["iface"], d["type"], props),
                    mac=props.get("GENERAL.HWADDR", [""])[0].lower(),
                    ip=addr.partition("/")[0], prefix=addr.partition("/")[2],
                    gateway=props.get("IP4.GATEWAY", [""])[0],
                    dns=props.get("IP4.DNS", []), method="", ssid="", signal=None)
        if d["connection"] == HOTSPOT:
            item["state"] = "hotspot"
        elif d["connection"]:
            item["method"] = ip_config(d["connection"])["method"]
        if d["type"] == "wifi" and d["state"] == "connected" and d["connection"] != HOTSPOT:
            out = nmcli("-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi", "list",
                        "ifname", d["iface"], "--rescan", "no").stdout
            for f in map(split_terse, out.splitlines()):
                if len(f) >= 3 and f[0] == "yes":
                    item["ssid"], item["signal"] = f[1], int(f[2] or 0)
        items.append(item)
    return items


# --------------------------------------------------------------------------- IP-Einstellungen
def ip_config(connection):
    """{method: 'auto'|'manual', address, prefix, gateway, dns: [..]} eines Verbindungsprofils."""
    props = terse_props(nmcli("-t", "-f", "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns",
                              "con", "show", connection).stdout)
    addr = props.get("ipv4.addresses", [""])[0].split(",")[0].strip()
    dns = [d.strip() for d in ",".join(props.get("ipv4.dns", [])).split(",") if d.strip()]
    return {"method": "manual" if props.get("ipv4.method", ["auto"])[0] == "manual" else "auto",
            "address": addr.partition("/")[0], "prefix": addr.partition("/")[2] or "24",
            "gateway": props.get("ipv4.gateway", [""])[0], "dns": dns}


def validate_ip_config(cfg):
    """Prüft und normalisiert die Eingabe. Gibt (cfg, None) oder (None, Fehlermeldung) zurück."""
    if cfg.get("method") != "manual":
        return {"method": "auto", "address": "", "prefix": "", "gateway": "", "dns": []}, None
    try:
        prefix = int(cfg.get("prefix") or 24)
        if not 1 <= prefix <= 30:
            raise ValueError
        iface = ipaddress.IPv4Interface(f"{str(cfg.get('address', '')).strip()}/{prefix}")
    except ValueError:
        return None, "Please enter a valid IP address and subnet (e.g. 192.168.1.50 / 24)."
    if iface.ip in (iface.network.network_address, iface.network.broadcast_address):
        return None, "This address is reserved in the subnet – please choose another one."
    gateway = str(cfg.get("gateway") or "").strip()
    if gateway:
        try:
            if ipaddress.IPv4Address(gateway) not in iface.network:
                return None, "The gateway (router) must be in the same subnet as the address."
        except ValueError:
            return None, "Please enter a valid gateway (router) address."
    dns = cfg.get("dns") or []
    if isinstance(dns, str):
        dns = re.split(r"[\s,;]+", dns.strip())
    try:
        dns = [str(ipaddress.IPv4Address(d)) for d in dns if d]
    except ValueError:
        return None, "Please enter valid DNS server addresses."
    return {"method": "manual", "address": str(iface.ip), "prefix": str(prefix),
            "gateway": gateway, "dns": dns or ([gateway] if gateway else [])}, None


def apply_ip_config(connection, cfg):
    if cfg["method"] == "manual":
        args = ["ipv4.addresses", f"{cfg['address']}/{cfg['prefix']}",
                "ipv4.gateway", cfg["gateway"], "ipv4.dns", " ".join(cfg["dns"]),
                "ipv4.method", "manual"]
    else:
        args = ["ipv4.method", "auto", "ipv4.gateway", "", "ipv4.addresses", "", "ipv4.dns", ""]
    r = nmcli("con", "mod", connection, *args)
    if r.returncode != 0:
        return r.stderr.strip() or "NetworkManager rejected the settings."
    r = nmcli("-w", "30", "con", "up", connection, timeout=45)
    return None if r.returncode == 0 else (r.stderr.strip() or "Could not activate the settings.")


class IpChange:
    """IP-Einstellungen mit Rücknahme-Frist (Schutz vor dem Aussperren)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending = None   # {connection, previous, deadline, timer}

    def start(self, connection, cfg):
        with self.lock:
            if self.pending:
                return "Another network change is still waiting for confirmation."
            previous = ip_config(connection)
            timer = threading.Timer(ROLLBACK_SECONDS, self.rollback)
            timer.daemon = True
            self.pending = {"connection": connection, "previous": previous, "timer": timer,
                            "applied": False, "deadline": time.time() + ROLLBACK_SECONDS}
            timer.start()
        # kurz warten, damit die HTTP-Antwort noch über die alte Adresse hinausgeht
        threading.Timer(1.5, self._apply, args=(connection, cfg)).start()
        return None

    def _apply(self, connection, cfg):
        err = apply_ip_config(connection, cfg)
        if err:
            log.warning("IP settings for '%s' failed (%s) – reverting", connection, err)
            self.rollback()
            return
        with self.lock:
            if self.pending:
                self.pending["applied"] = True
        log.info("IP settings for '%s' applied – waiting for confirmation", connection)

    def confirm(self):
        """Bestätigt die neuen Werte. Wer das aufrufen kann, erreicht das Gerät noch."""
        with self.lock:
            if not self.pending or not self.pending["applied"]:
                return False
            self.pending["timer"].cancel()
            log.info("IP settings for '%s' confirmed", self.pending["connection"])
            self.pending = None
            return True

    def rollback(self):
        with self.lock:
            p, self.pending = self.pending, None
        if not p:
            return
        p["timer"].cancel()
        log.warning("IP settings for '%s' not confirmed – restoring the previous ones",
                    p["connection"])
        apply_ip_config(p["connection"], p["previous"])

    def state(self):
        with self.lock:
            if not self.pending:
                return None
            return {"connection": self.pending["connection"],
                    "applied": self.pending["applied"],
                    "seconds_left": max(0, int(self.pending["deadline"] - time.time()))}


# --------------------------------------------------------------------------- WLAN + Hotspot
class Net:
    def __init__(self, conf):
        self.ap_ssid = conf.get("AP_SSID", "OnviPi-Setup")
        self.ap_password = conf.get("AP_PASSWORD", "")
        self.wanted = conf.get("WIFI_IFACE", "wlan0")
        self.iface = self.wanted      # Gerät fürs Heimnetz
        self.ap_iface = self.wanted   # Gerät für den Einrichtungs-Hotspot
        self.refresh()

    def refresh(self):
        """WLAN-Geräte neu einlesen (USB-Adapter kann jederzeit kommen oder gehen)."""
        wifi = wifi_devices()
        names = [d["iface"] for d in wifi]
        if names:
            self.iface = self.wanted if self.wanted in names else names[0]
            builtin = [d["iface"] for d in wifi if not d["usb"]]
            self.ap_iface = builtin[0] if builtin else self.iface
        return wifi

    def select(self, iface):
        self.wanted = iface
        self.refresh()

    @property
    def independent(self):
        """Hotspot und Heimnetz laufen auf verschiedenen Geräten – der Hotspot kann anbleiben."""
        return self.ap_iface != self.iface

    def active(self):
        out = nmcli("-t", "-f", "NAME,TYPE,DEVICE", "con", "show", "--active").stdout
        return [split_terse(line) for line in out.splitlines() if line]

    def online(self):
        for fields in self.active():
            if len(fields) >= 2 and fields[0] != HOTSPOT and fields[1] in WIFI_TYPES + ETH_TYPES:
                return True
        return False

    def hotspot_active(self):
        return any(f and f[0] == HOTSPOT for f in self.active())

    def saved_wifi(self):
        out = nmcli("-t", "-f", "NAME,TYPE", "con", "show").stdout
        return [f[0] for f in map(split_terse, out.splitlines())
                if len(f) >= 2 and f[1] in WIFI_TYPES and f[0] != HOTSPOT]

    def forget(self, name):
        if name == HOTSPOT or name not in self.saved_wifi():
            return False
        return nmcli("con", "delete", name).returncode == 0

    def scan(self, iface=None):
        """[(ssid, signal, security)] nach Signalstärke. Im AP-Modus kann dasselbe Gerät nicht scannen."""
        iface = iface or self.iface
        nmcli("dev", "wifi", "rescan", "ifname", iface, timeout=20)
        time.sleep(4)
        out = nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list",
                    "ifname", iface, timeout=20).stdout
        best = {}
        for fields in map(split_terse, out.splitlines()):
            if len(fields) < 3 or not fields[0] or fields[0] == self.ap_ssid:
                continue
            ssid, signal_, sec = fields[0], int(fields[1] or 0), fields[2]
            if ssid not in best or best[ssid][0] < signal_:
                best[ssid] = (signal_, sec)
        nets = sorted(((s, v[0], v[1]) for s, v in best.items()), key=lambda n: -n[1])
        log.info("Scan on %s: %d networks", iface, len(nets))
        return nets

    def ensure_hotspot_profile(self):
        nmcli("con", "delete", HOTSPOT)  # immer frisch aus onvipi.conf anlegen
        args = ["con", "add", "type", "wifi", "ifname", self.ap_iface, "con-name", HOTSPOT,
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
        self.refresh()
        self.ensure_hotspot_profile()
        ok = nmcli("con", "up", HOTSPOT, timeout=40).returncode == 0
        log.info("Hotspot '%s' on %s %s", self.ap_ssid, self.ap_iface,
                 "active" if ok else "failed to start")
        return ok

    def hotspot_down(self):
        nmcli("con", "down", HOTSPOT, timeout=20)

    def clients_connected(self):
        out = run(["iw", "dev", self.ap_iface, "station", "dump"], timeout=5).stdout
        return "Station" in out

    def wait_online(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if self.online():
                return True
            time.sleep(2)
        return False

    def wifi_connected(self):
        return any(d["iface"] == self.iface and d["state"] == "connected"
                   and d["connection"] != HOTSPOT for d in devices())

    def connect_new(self, ssid, psk, hidden):
        """Neues WLAN verbinden. Gibt (ok, Fehlermeldung) zurück.

        Läuft der Hotspot auf demselben Gerät, wird er dafür abgeschaltet (der Aufrufer
        startet ihn bei Misserfolg wieder). Ein vorher aktives WLAN wird bei Misserfolg
        wiederhergestellt.
        """
        name = "wifi-" + re.sub(r"[^A-Za-z0-9_.-]+", "_", ssid)[:40]
        previous = next((d["connection"] for d in devices()
                         if d["iface"] == self.iface and d["state"] == "connected"
                         and d["connection"] not in ("", HOTSPOT, name)), None)
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
            return False, r.stderr.strip() or "could not create connection profile"
        if not self.independent:
            self.hotspot_down()
        r = nmcli("-w", str(CONNECT_TIMEOUT), "con", "up", name, timeout=CONNECT_TIMEOUT + 15)
        if r.returncode == 0:
            for _ in range(5):
                if self.wifi_connected():
                    log.info("Connected to '%s' on %s", ssid, self.iface)
                    return True, ""
                time.sleep(2)
        log.warning("Connecting to '%s' failed: %s", ssid, r.stderr.strip())
        nmcli("con", "delete", name)
        if previous:
            nmcli("con", "up", previous, timeout=CONNECT_TIMEOUT)
        return False, r.stderr.strip()
