#!/usr/bin/env python3
"""WS-Discovery-Responder für den ONVIF-Server (onvipi_onvif).

Dieser kleine Dienst antwortet auf Multicast-Probes (239.255.255.250:3702), damit Home
Assistant ("ONVIF hinzufügen") und andere ONVIF-Clients die Kameras finden. Jede aktive
Kamera aus cameras.json wird als eigenes Gerät gemeldet.
"""
import logging
import os
import random
import re
import socket
import struct
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from onvipilib import ipv4_addresses  # noqa: E402
from onvipi_camera import camera_uuid, effective_mac, load_cameras  # noqa: E402

MCAST_GRP = "239.255.255.250"
PORT = int(os.environ.get("ONVIPI_WSD_PORT", "3702"))
log = logging.getLogger("onvipi-wsd")

ENVELOPE = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl" xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
<s:Header>
<a:MessageID>urn:uuid:{msg_id}</a:MessageID>{relates}
<a:To>{to}</a:To>
<a:Action>{action}</a:Action>
<d:AppSequence InstanceId="{instance}" MessageNumber="{number}"/>
</s:Header>
<s:Body>{body}</s:Body>
</s:Envelope>"""

MATCH = """<d:{tag}>
<a:EndpointReference><a:Address>urn:uuid:{dev}</a:Address></a:EndpointReference>
<d:Types>dn:NetworkVideoTransmitter tds:Device</d:Types>
<d:Scopes>{scopes}</d:Scopes>
<d:XAddrs>{xaddrs}</d:XAddrs>
<d:MetadataVersion>1</d:MetadataVersion>
</d:{tag}>"""

ACTION_PROBE = "http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe"
ACTION_MATCHES = "http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches"
ACTION_HELLO = "http://schemas.xmlsoap.org/ws/2005/04/discovery/Hello"
ANON = "http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous"
DISCOVERY_TO = "urn:schemas-xmlsoap-org:ws:2005:04:discovery"


def tag_text(xml, name):
    m = re.search(r"<(?:[\w-]+:)?%s\b[^>]*>([^<]*)</" % name, xml)
    return m.group(1).strip() if m else None


class Responder:
    def __init__(self):
        self.cams = [c for c in load_cameras() if c["enabled"]]
        self.instance = int(time.time())
        self.number = 0
        self.joined = set()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        self.sock.bind(("", PORT))
        self.sock.settimeout(5)

    def join_groups(self):
        """Tritt der Multicast-Gruppe auf jeder (neuen) Schnittstelle bei."""
        addrs = set(ipv4_addresses()) or {"0.0.0.0"}
        for addr in addrs - self.joined:
            mreq = struct.pack("4s4s", socket.inet_aton(MCAST_GRP), socket.inet_aton(addr))
            try:
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                log.info("Multicast active on %s", addr)
            except OSError as e:
                if e.errno != 98:  # EADDRINUSE = schon beigetreten
                    log.debug("Join on %s failed: %s", addr, e)
                    continue
            self.joined.add(addr)
            self.hello(addr)
        self.joined &= addrs

    def message(self, action, body, relates=None, to=ANON):
        self.number += 1
        return ENVELOPE.format(
            msg_id=uuid.uuid4(), to=to, action=action, instance=self.instance,
            number=self.number, body=body,
            relates=f"\n<a:RelatesTo>{relates}</a:RelatesTo>" if relates else "",
        ).encode()

    @staticmethod
    def scopes(cam):
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", cam["name"]) or "OnviPi"
        scopes = [
            "onvif://www.onvif.org/Profile/Streaming",
            "onvif://www.onvif.org/type/video_encoder",
            "onvif://www.onvif.org/type/Network_Video_Transmitter",
            f"onvif://www.onvif.org/name/{name}",
            "onvif://www.onvif.org/hardware/RaspberryPi",
        ]
        mac = effective_mac(cam)
        if mac:  # HA übernimmt die MAC als ID → bereits eingerichtete Kamera wird ausgeblendet
            scopes.append(f"onvif://www.onvif.org/MAC/{mac}")
        return " ".join(scopes)

    def match(self, tag, ip, cam):
        xaddrs = f"http://{ip}:{cam['port']}/onvif/device_service"
        return MATCH.format(tag=tag, dev=camera_uuid(cam), scopes=self.scopes(cam),
                            xaddrs=xaddrs)

    def hello(self, ip):
        if ip == "0.0.0.0":
            return
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            for cam in self.cams:
                msg = self.message(ACTION_HELLO, self.match("Hello", ip, cam), to=DISCOVERY_TO)
                self.sock.sendto(msg, (MCAST_GRP, PORT))
        except OSError as e:
            log.debug("Hello on %s failed: %s", ip, e)

    @staticmethod
    def local_ip_for(peer):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((peer, 9))
            return s.getsockname()[0]
        finally:
            s.close()

    @staticmethod
    def wants_us(xml):
        types = tag_text(xml, "Types")
        if not types:
            return True  # leere Probe = alle Geräte
        names = {t.split(":")[-1] for t in types.split()}
        return bool(names & {"NetworkVideoTransmitter", "Device"})

    def handle(self, data, peer):
        xml = data.decode("utf-8", "replace")
        if (tag_text(xml, "Action") != ACTION_PROBE or not self.wants_us(xml)
                or not self.cams):
            return
        msg_id = tag_text(xml, "MessageID")
        ip = self.local_ip_for(peer[0])
        matches = "".join(self.match("ProbeMatch", ip, cam) for cam in self.cams)
        reply = self.message(ACTION_MATCHES, f"<d:ProbeMatches>{matches}</d:ProbeMatches>",
                             relates=msg_id)
        time.sleep(random.uniform(0, 0.3))  # laut Spezifikation leicht verzögert antworten
        self.sock.sendto(reply, peer)
        log.info("Answered probe from %s (%s)", peer[0], ip)

    def run(self):
        last_join = 0
        while True:
            if time.time() - last_join > 30:
                self.join_groups()
                last_join = time.time()
            try:
                data, peer = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                self.handle(data, peer)
            except Exception:  # ein kaputtes Paket darf den Dienst nicht beenden
                log.exception("Error handling packet from %s", peer)


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"),
                        format="%(name)s: %(message)s")
    Responder().run()
