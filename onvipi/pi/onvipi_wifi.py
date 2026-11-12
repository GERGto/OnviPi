#!/usr/bin/env python3
"""OnviPi-Netzwerkdienst: Weboberfläche, Einrichtungs-Hotspot und Assistent.

Ablauf:
  * Die Weboberfläche (onvipi_web) läuft immer auf Port 80.
  * Noch kein Konto eingerichtet (frisch geflashtes Image, oder Passwort über die Datei
    "onvipi-reset" zurückgesetzt): Der Assistent ist offen – über den Hotspot "AP_SSID"
    (http://10.42.0.1, Captive Portal) und, wenn schon ein Netz besteht, über das LAN.
    Der Assistent sammelt alle Angaben; erst zum Schluss – nachdem der Nutzer bestätigt
    hat, dass er die Zugangsdaten notiert hat – wird das WLAN verbunden und das Konto
    angelegt. Schlägt das WLAN fehl, kommt der Hotspot mit einer Fehlermeldung zurück.
  * Eingerichtet, aber kein Netz: bis zu BOOT_GRACE Sekunden warten, danach nach
    OFFLINE_GRACE Sekunden ohne Netz den Hotspot starten. Dort öffnet sich die normale
    Oberfläche mit Anmeldung; unter "Network" lässt sich ein anderes WLAN wählen.
  * Ist am Hotspot niemand verbunden, wird alle RETRY_INTERVAL Sekunden erneut das
    gespeicherte WLAN versucht (z. B. nach Stromausfall, wenn der Router langsamer
    bootet als der Pi).
  * Steckt ein USB-WLAN-Adapter, kann er fürs Heimnetz gewählt werden; der Hotspot bleibt
    dann auf dem eingebauten WLAN und während des Verbindens an.

Befehle:
  onvipi_wifi.py run      Dienst (systemd)
  onvipi_wifi.py status   Zustand anzeigen
  onvipi_wifi.py reset    gespeicherte WLANs löschen -> Hotspot zum Neuverbinden
  onvipi_wifi.py portal   Hotspot sofort starten (auch wenn der Pi gerade online ist)
"""
import argparse
import logging
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from onvipilib import CONF, load_conf, primary_ip, save_conf  # noqa: E402
import onvipi_camera as camera  # noqa: E402
import onvipi_net as netlib  # noqa: E402
import onvipi_system as system  # noqa: E402
from onvipi_web import App, restart_services  # noqa: E402

log = logging.getLogger("onvipi-wifi")

BOOT_GRACE = 60
OFFLINE_GRACE = 300
RETRY_INTERVAL = 300
DONE_GRACE = 90      # so lange bleibt der Hotspot nach der Einrichtung noch an


def wait(app, seconds):
    """Schlafen, aber bei einem neuen Auftrag aus der Oberfläche sofort aufwachen."""
    app.wake.wait(seconds)
    app.wake.clear()


def station_on(iface):
    """Ist dieses WLAN-Gerät gerade mit einem Netz verbunden (nicht als Hotspot)?"""
    return any(d["iface"] == iface and d["state"] == "connected"
               and d["connection"] != netlib.HOTSPOT for d in netlib.devices())


def start_hotspot(app):
    net = app.net
    net.refresh()
    if net.ap_iface == net.iface or not app.networks:
        app.networks = net.scan() or app.networks   # vorher – im AP-Modus geht das nicht
    app.ap = net.hotspot_up()
    return app.ap


def process_wifi_job(app):
    """WLAN-Auftrag aus der Oberfläche ausführen. None = keiner, sonst Erfolg True/False."""
    job = app.take_wifi_job()
    if not job:
        return None
    ssid = job[0]
    time.sleep(2)  # Antwort an den Browser noch ausliefern lassen
    ok, _ = app.net.connect_new(*job)
    if ok:
        app.wifi_status = {"phase": "done", "ssid": ssid, "error": ""}
        restart_services("onvipi-mqtt.service")
    else:
        app.wifi_status = {"phase": "failed", "ssid": ssid, "error": (
            f"Could not connect to “{ssid}”. Please check the Wi-Fi password.")}
    return ok


# --------------------------------------------------------------------------- Einrichtung
def run_setup(app, job):
    """Letzter Schritt des Assistenten: Region, WLAN, Konto. True, wenn eingerichtet."""
    net = app.net
    conf = load_conf(CONF)
    if job["country"] != conf.get("WIFI_COUNTRY"):
        system.set_country(job["country"])
        conf["WIFI_COUNTRY"] = job["country"]
        save_conf(CONF, conf)
        log.info("Wi-Fi country: %s", job["country"])
    if job["timezone"] != system.current_timezone():
        system.set_timezone(job["timezone"])
        log.info("Time zone: %s", job["timezone"])

    wifi = job["wifi"]
    if wifi:
        app.select_wifi_iface(wifi["iface"])
        time.sleep(2)  # Antwort an den Browser noch ausliefern lassen
        ok, _ = net.connect_new(wifi["ssid"], wifi["psk"], wifi["hidden"])
        if not ok:
            app.setup_progress = {"phase": "failed", "error": (
                f"Could not connect to “{wifi['ssid']}”. Please check the Wi-Fi password "
                "and try again.")}
            return False

    err = system.create_login(job["user"], job["password"])
    if err:
        log.error("Creating the account failed: %s", err)
        app.setup_progress = {"phase": "failed", "error": err}
        return False
    conf = load_conf(CONF)
    conf.update(CAM_USER=job["user"], CAM_PASSWORD=job["password"], LOGIN_USER=job["user"],
                UPDATE_CHECK="1" if job.get("update_check", True) else "0",
                UPDATE_AUTO="1" if job.get("update_install", True) else "0")
    save_conf(CONF, conf)
    app.setup_draft = {}
    log.info("Setup finished: account '%s' created, %s", job["user"],
             f"Wi-Fi '{wifi['ssid']}'" if wifi else "stays on Ethernet")
    pin_camera_ids()
    restart_services()   # go2rtc/ONVIF übernehmen das neue Passwort
    app.setup_progress = {"phase": "done", "error": "", "ip": primary_ip()}
    return True


def setup_mode(app):
    """Kein Konto: Assistent anbieten, bis er abgeschlossen ist."""
    net = app.net
    net.ap_password = load_conf(CONF).get("AP_PASSWORD", "")   # offen, solange nichts gesetzt ist
    net.refresh()
    # Trägt das Hotspot-Gerät gerade das Heimnetz (Passwort zurückgesetzt), bleibt es
    # verbunden – der Assistent ist dann über das LAN erreichbar.
    hotspot = not station_on(net.ap_iface)
    if hotspot:
        start_hotspot(app)
    log.info("No account set up yet – setup wizard is open%s",
             f" (hotspot '{net.ap_ssid}')" if hotspot else " on the local network")
    try:
        while not system.configured():
            job = app.take_setup_job()
            if job and run_setup(app, job):
                break
            if hotspot and not net.hotspot_active():
                start_hotspot(app)   # z. B. nach einem fehlgeschlagenen WLAN-Versuch
            wait(app, 2)
        # Ergebnis-Seite noch anzeigen lassen, dann den Hotspot schließen
        end = time.time() + DONE_GRACE
        while net.hotspot_active() and time.time() < end and net.clients_connected():
            time.sleep(3)
    finally:
        net.hotspot_down()
        app.ap = False


# --------------------------------------------------------------------------- Netz verloren
def recovery_mode(app, until_online=True):
    """Eingerichtet, aber ohne Netz: Hotspot mit der normalen Oberfläche (Anmeldung).

    Der Hotspot ist jetzt mit WPA2 verschlüsselt, Schlüssel = Passwort des Kontos (bzw.
    AP_PASSWORD). In einem offenen WLAN könnte sonst jeder in Reichweite das Passwort
    mitlesen, das der Besitzer auf der Anmeldeseite eintippt – und ein Angreifer kann den
    Hotspot erzwingen, indem er das Heim-WLAN stört.
    """
    net = app.net
    conf = load_conf(CONF)
    net.ap_password = conf.get("AP_PASSWORD") or conf.get("CAM_PASSWORD", "")[:63]
    start_hotspot(app)
    last_retry = time.time()
    try:
        while True:
            if process_wifi_job(app):
                return
            if until_online and net.online():   # z. B. Netzwerkkabel eingesteckt
                log.info("Network is back – closing the hotspot")
                return
            if not net.hotspot_active():
                start_hotspot(app)
            if (until_online and net.saved_wifi() and not net.independent
                    and time.time() - last_retry > RETRY_INTERVAL
                    and not net.clients_connected()):
                log.info("Nobody on the hotspot – retrying saved Wi-Fi")
                net.hotspot_down()
                if net.wait_online(netlib.CONNECT_TIMEOUT):
                    return
                start_hotspot(app)
                last_retry = time.time()
            wait(app, 2)
    finally:
        net.hotspot_down()
        app.ap = False


def pin_camera_ids():
    """Kameras ihre feste ID (MAC) geben, sobald ein Netz da ist – siehe onvipi_camera."""
    try:
        cams = camera.load_cameras()
        if cams and camera.pin_mac(cams):
            camera.save_cameras(cams)
            return True
    except OSError as ex:
        log.debug("Could not store camera IDs: %s", ex)
    return False


def service():
    if system.consume_reset_marker():
        log.warning("Reset file found – the password was cleared, the setup wizard will open")
    net = netlib.Net(load_conf(CONF))
    app = App(net)

    def stop(*_):
        app.stop()
        net.hotspot_down()
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)

    net.hotspot_down()  # Reste eines früheren Laufs
    app.start()
    offline_since = None
    if system.configured():
        log.info("Waiting up to %ds for a network …", BOOT_GRACE)
        offline_since = None if net.wait_online(BOOT_GRACE) else time.time() - OFFLINE_GRACE
    while True:
        if not system.configured():
            setup_mode(app)
            offline_since = None
            continue
        process_wifi_job(app)
        if net.online():
            offline_since = None
            if pin_camera_ids():
                restart_services("onvipi-onvif.service", "onvipi-wsd.service")
            wait(app, 15)
            continue
        offline_since = offline_since or time.time()
        if time.time() - offline_since < OFFLINE_GRACE:
            wait(app, 10)
            continue
        log.info("No network – starting the hotspot")
        recovery_mode(app)
        offline_since = None


def main():
    ap = argparse.ArgumentParser(description="OnviPi web interface, hotspot and setup wizard")
    ap.add_argument("command", choices=["run", "status", "reset", "portal"],
                    nargs="?", default="run")
    args = ap.parse_args()
    logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"), format="%(name)s: %(message)s")
    net = netlib.Net(load_conf(CONF))

    if args.command == "run":
        service()
    elif args.command == "portal":
        # Hotspot sofort starten, unabhängig von LAN/WLAN (zum Testen oder um ein anderes
        # WLAN zu wählen). Läuft bis Strg-C oder bis eine WLAN-Verbindung klappt.
        # Der Dienst belegt denselben Port – solange pausieren.
        netlib.run(["systemctl", "stop", "onvipi-wifi.service"], timeout=30)
        app = App(net)

        def stop(*_):
            app.stop()
            net.hotspot_down()
            netlib.run(["systemctl", "start", "onvipi-wifi.service"], timeout=30)
            sys.exit(0)
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        log.info("Starting hotspot '%s' on demand …", net.ap_ssid)
        app.start()
        if system.configured():
            recovery_mode(app, until_online=False)
        else:
            setup_mode(app)
        log.info("Hotspot closed.")
        stop()
    elif args.command == "status":
        print("Online:", net.online())
        print("Hotspot active:", net.hotspot_active())
        print("Wi-Fi adapter (home network):", net.iface)
        print("Saved Wi-Fi networks:", ", ".join(net.saved_wifi()) or "–")
        print("Account set up:", "yes" if system.configured() else "no (setup wizard is open)")
        for cam in camera.load_cameras():
            print(f"Camera {cam['id']}: {cam['name']} – "
                  f"{'on' if cam['enabled'] else 'off'}, ONVIF port {cam['port']}")
    elif args.command == "reset":
        for name in net.saved_wifi():
            netlib.nmcli("con", "delete", name)
            print("deleted:", name)
        netlib.run(["systemctl", "restart", "onvipi-wifi.service"])
        print("The hotspot is starting (Wi-Fi will disconnect).")


if __name__ == "__main__":
    main()
