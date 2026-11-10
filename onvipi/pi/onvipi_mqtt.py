#!/usr/bin/env python3
"""Meldet die OnviPi per MQTT-Discovery automatisch in Home Assistant an.

Legt in HA ein Gerät an mit:
  * Kamera "Schnappschuss" (Standbild alle SNAPSHOT_INTERVAL Sekunden, 0 = aus)
  * Diagnose: CPU-Temperatur, WLAN-Signal, IP-Adresse, ONVIF-Adresse
  * Taste "Neustart"
Ohne /etc/onvipi/mqtt.conf wartet der Dienst einfach, bis Zugangsdaten da sind.
"""
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from onvipilib import CONF, MQTT_CONF, STREAM_NAME, device_id, load_conf, primary_ip  # noqa: E402

log = logging.getLogger("onvipi-mqtt")
DISCOVERY = "homeassistant"
STATS_INTERVAL = 60


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def cpu_temp():
    raw = read("/sys/class/thermal/thermal_zone0/temp").strip()
    return round(int(raw) / 1000, 1) if raw.isdigit() else None


def wifi_signal():
    for line in read("/proc/net/wireless").splitlines()[2:]:
        parts = line.split()
        if len(parts) > 3:
            try:
                return int(float(parts[3].rstrip(".")))
            except ValueError:
                pass
    return None


def snapshot():
    url = f"http://127.0.0.1:1984/api/frame.jpeg?src={STREAM_NAME}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.read()
    except OSError as e:
        log.warning("Snapshot failed: %s", e)
        return None


class Announcer:
    def __init__(self, conf, mqtt_conf):
        import paho.mqtt.client as mqtt
        self.conf, self.mqtt_conf = conf, mqtt_conf
        # "picam_" = historisches Präfix (PiCam) – NICHT ändern, sonst neue Entitäten in HA
        self.uid = "picam_" + device_id().replace("-", "")[:12]
        self.base = f"onvipi/{self.uid}"
        self.interval = int(mqtt_conf.get("SNAPSHOT_INTERVAL", "30") or 0)
        try:  # paho-mqtt 2.x (Trixie) und 1.x (Bookworm)
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.uid)
        except AttributeError:
            self.client = mqtt.Client(client_id=self.uid)
        if mqtt_conf.get("MQTT_USER"):
            self.client.username_pw_set(mqtt_conf["MQTT_USER"], mqtt_conf.get("MQTT_PASSWORD", ""))
        self.client.will_set(f"{self.base}/status", "offline", retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.reconnect_delay_set(5, 120)

    def device(self):
        dev = {
            "identifiers": [self.uid],
            "name": self.conf.get("CAM_NAME", "OnviPi"),
            "manufacturer": "Raspberry Pi",
            "model": "OnviPi (Raspberry Pi + USB webcam)",
        }
        ip = primary_ip()
        if ip:
            dev["configuration_url"] = f"http://{ip}:1984/"
        return dev

    def publish_discovery(self):
        dev = self.device()
        avail = {"availability_topic": f"{self.base}/status"}
        entities = []
        if self.interval > 0:
            entities.append(("camera", "snapshot", {
                "name": "Snapshot", "topic": f"{self.base}/snapshot"}))
        entities += [
            ("sensor", "cpu_temp", {
                "name": "CPU temperature", "state_topic": f"{self.base}/stats",
                "value_template": "{{ value_json.cpu_temp }}", "unit_of_measurement": "°C",
                "device_class": "temperature", "state_class": "measurement",
                "entity_category": "diagnostic"}),
            ("sensor", "wifi_signal", {
                "name": "Wi-Fi signal", "state_topic": f"{self.base}/stats",
                "value_template": "{{ value_json.wifi_signal }}", "unit_of_measurement": "dBm",
                "device_class": "signal_strength", "state_class": "measurement",
                "entity_category": "diagnostic"}),
            ("sensor", "ip", {
                "name": "IP address", "state_topic": f"{self.base}/stats",
                "value_template": "{{ value_json.ip }}", "entity_category": "diagnostic",
                "icon": "mdi:ip-network"}),
            ("sensor", "onvif", {
                "name": "ONVIF address", "state_topic": f"{self.base}/stats",
                "value_template": "{{ value_json.onvif }}", "entity_category": "diagnostic",
                "icon": "mdi:cctv"}),
            ("button", "reboot", {
                "name": "Restart", "command_topic": f"{self.base}/cmd",
                "payload_press": "reboot", "device_class": "restart",
                "entity_category": "config"}),
        ]
        for component, key, cfg in entities:
            cfg.update(avail, unique_id=f"{self.uid}_{key}", device=dev)
            self.client.publish(f"{DISCOVERY}/{component}/{self.uid}/{key}/config",
                                json.dumps(cfg), retain=True)
        if self.interval <= 0:  # Kamera-Entität entfernen, falls abgeschaltet
            self.client.publish(f"{DISCOVERY}/camera/{self.uid}/snapshot/config", "", retain=True)
        self.client.publish(f"{self.base}/status", "online", retain=True)
        log.info("Discovery sent to Home Assistant (%s)", self.uid)

    def on_connect(self, client, userdata, flags, rc, properties=None):
        if getattr(rc, "is_failure", rc != 0):
            log.error("MQTT login rejected: %s", rc)
            return
        client.subscribe(f"{self.base}/cmd")
        client.subscribe(f"{DISCOVERY}/status")
        self.publish_discovery()
        self.publish_stats()

    def on_message(self, client, userdata, msg):
        payload = msg.payload.decode(errors="replace")
        if msg.topic == f"{DISCOVERY}/status" and payload == "online":
            self.publish_discovery()  # HA wurde neu gestartet
        elif msg.topic == f"{self.base}/cmd" and payload == "reboot":
            log.info("Restart requested via Home Assistant")
            client.publish(f"{self.base}/status", "offline", retain=True)
            subprocess.Popen(["systemctl", "reboot"])

    def publish_stats(self):
        ip = primary_ip()
        self.client.publish(f"{self.base}/stats", json.dumps({
            "cpu_temp": cpu_temp(), "wifi_signal": wifi_signal(), "ip": ip,
            "onvif": f"{ip}:1984" if ip else None,
        }), retain=True)

    def run(self):
        host = self.mqtt_conf.get("MQTT_HOST", "homeassistant.local")
        port = int(self.mqtt_conf.get("MQTT_PORT", "1883"))
        while True:
            try:
                self.client.connect(host, port, keepalive=60)
                break
            except OSError as e:
                log.warning("MQTT %s:%s unreachable (%s) – retrying in 30 s", host, port, e)
                time.sleep(30)
        self.client.loop_start()
        last_stats = last_snap = 0
        while True:
            now = time.time()
            if self.client.is_connected():
                if now - last_stats >= STATS_INTERVAL:
                    self.publish_stats()
                    last_stats = now
                if self.interval > 0 and now - last_snap >= self.interval:
                    img = snapshot()
                    if img:
                        self.client.publish(f"{self.base}/snapshot", img, retain=True)
                    last_snap = now
            time.sleep(1)


def main():
    logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"), format="%(name)s: %(message)s")
    while not load_conf(MQTT_CONF).get("MQTT_USER"):
        time.sleep(30)  # wird über das Captive Portal oder von Hand angelegt
    Announcer(load_conf(CONF), load_conf(MQTT_CONF)).run()


if __name__ == "__main__":
    main()
