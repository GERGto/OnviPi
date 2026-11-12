#!/usr/bin/env python3
"""Eigener ONVIF-Server für OnviPi: Device + Media + Events (Bewegung).

go2rtcs eingebauter ONVIF-Server kann keine Events und lässt sich nicht erweitern.
Dieser schlanke Server spricht die ONVIF-SOAP-Operationen selbst, die die
Home-Assistant-ONVIF-Integration braucht, und verweist fürs Video auf go2rtc:

  * Device : GetSystemDateAndTime, GetDeviceInformation, GetCapabilities, GetServices,
             GetScopes, GetServiceCapabilities, GetNetworkInterfaces (MAC = ID in HA),
             SystemReboot, SetSystemDateAndTime (die beiden Tasten, die HA immer anlegt)
  * Media  : GetProfiles, GetVideoSources, GetStreamUri (-> go2rtc RTSP),
             GetSnapshotUri (-> go2rtc frame.jpeg), GetServiceCapabilities
  * Events : GetEventProperties, GetServiceCapabilities, CreatePullPointSubscription,
             PullMessages, Renew, Unsubscribe, SetSynchronizationPoint
             -> Topic tns1:RuleEngine/CellMotionDetector/Motion (IsMotion true/false)

Die Bewegung kommt aus einer lokalen Analyse: ein ffmpeg zieht den RTSP-Stream,
bildet die Differenz aufeinanderfolgender Bilder und misst deren mittlere Helligkeit
(signalstats.YAVG). Schwelle aus MOTION_SENSITIVITY (0-100). Der Analyse-ffmpeg läuft
nur, solange mindestens eine Event-Subscription aktiv ist (spart CPU).

Jede aktive Kamera aus cameras.json ist ein eigenes ONVIF-Gerät auf ihrem eigenen Port
(ab ONVIF_PORT, Standard 8000) mit eigener MAC/ID. onvipi_wsd verweist darauf.
"""
import base64
import calendar
import hashlib
import hmac
import http.server
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from onvipilib import CONF, load_conf, primary_interface, primary_ip  # noqa: E402
from onvipi_camera import (camera_uuid, effective_mac, get_camera, load_cameras,  # noqa: E402
                           output_size, sub_id, sub_size)

log = logging.getLogger("onvipi-onvif")
PRIV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "onvipi_priv.py")
RUN_DIR = os.environ.get("ONVIPI_RUN_DIR", "/run/onvipi")   # RuntimeDirectory der Unit
RTSP_PORT = 8554
GO2RTC_API = 1984

ENVELOPE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
    ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
    ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
    ' xmlns:tev="http://www.onvif.org/ver10/events/wsdl"'
    ' xmlns:tt="http://www.onvif.org/ver10/schema"'
    ' xmlns:tns1="http://www.onvif.org/ver10/topics"'
    ' xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"'
    ' xmlns:wsa="http://www.w3.org/2005/08/addressing"'
    ' xmlns:wstop="http://docs.oasis-open.org/wsn/t-1"'
    ' xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
    "<s:Body>{body}</s:Body></s:Envelope>"
)


def iso(offset=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset))


def zone_mask(zones, w, h):
    """Indizes der Pixel (w×h-Graubild), die in einer der Zonen liegen. Leer = ganzes Bild."""
    if not zones:
        return None
    idx = set()
    for z in zones:
        x0, x1 = int(z["x"] * w), max(int(z["x"] * w) + 1, int(round((z["x"] + z["w"]) * w)))
        y0, y1 = int(z["y"] * h), max(int(z["y"] * h) + 1, int(round((z["y"] + z["h"]) * h)))
        for y in range(y0, min(y1, h)):
            idx.update(range(y * w + x0, y * w + min(x1, w)))
    return sorted(idx) or None


def recording_wants_motion(conf):
    """Aufzeichnung "bei Bewegung" braucht die Analyse auch ohne Home-Assistant-Abo."""
    return conf.get("REC_MODE") == "motion" and bool(conf.get("REC_UUID"))


def read_exact(f, n):
    """Genau n Bytes lesen (ein komplettes Graubild), sonst b'' bei Stream-Ende."""
    data = b""
    while len(data) < n:
        chunk = f.read(n - len(data))
        if not chunk:
            return b""
        data += chunk
    return data


# --------------------------------------------------------------------------- Bewegung
class Motion:
    """Lokale Bewegungserkennung per ffmpeg-Framedifferenz (signalstats.YAVG)."""

    def __init__(self, cam_id, sub_count):
        self.cam_id = cam_id
        self.sub_count = sub_count          # callable -> Anzahl aktiver Subscriptions
        self.state = False
        self.seq = 0                        # erhöht sich bei jedem Zustandswechsel
        self.cond = threading.Condition()
        self.proc = None
        self.marker = os.path.join(RUN_DIR, f"motion-{cam_id}")
        self.marked = 0
        threading.Thread(target=self._loop, daemon=True).start()

    def mark(self):
        """Solange Bewegung anliegt, die Marker-Datei aktuell halten (liest onvipi_record)."""
        now = time.time()
        if now - self.marked >= 1:
            self.marked = now
            try:
                with open(self.marker, "w") as f:
                    f.write(str(int(now)))
            except OSError:
                pass

    def set_state(self, val):
        with self.cond:
            if val != self.state:
                self.state = val
                self.seq += 1
                self.cond.notify_all()
                log.info("[%s] Motion %s", self.cam_id, "detected" if val else "ended")

    def wait_change(self, last_seq, timeout):
        with self.cond:
            if self.seq == last_seq:
                self.cond.wait(timeout)
            return self.seq, self.state

    @staticmethod
    def threshold(conf, cam):
        t = conf.get("MOTION_THRESHOLD", "")
        if t:  # expliziter Absolutwert (für Feintuning/Tests)
            try:
                return float(t)
            except ValueError:
                pass
        s = cam["sensitivity"]
        # Mittlere Pixeldifferenz (MAFD, 0-255): Rauschen ~0,3-0,8, Bewegung deutlich höher.
        return 20.0 - (s / 100.0) * (20.0 - 1.5)  # 0 -> 20 (stumpf), 100 -> 1.5 (scharf)

    def _loop(self):
        while True:
            conf = load_conf(CONF)
            cam = get_camera(load_cameras(conf), self.cam_id)
            wanted = self.sub_count() > 0 or recording_wants_motion(conf)
            if not cam or not cam["motion"] or not wanted:
                self.set_state(False)
                time.sleep(2)
                continue
            try:
                self._analyze(conf, cam)
            except Exception:
                log.exception("[%s] Motion analysis aborted", self.cam_id)
                time.sleep(3)
            self.set_state(False)

    def _analyze(self, conf, cam):
        # ffmpeg liefert winzige Graubilder als Rohdaten; die Differenz rechnen wir selbst.
        # Textbasierte Metadaten puffert ffmpeg bis zum Prozessende – Rohframes nicht.
        thr = self.threshold(conf, cam)
        fps = conf.get("MOTION_FPS", "5")
        cooldown = float(conf.get("MOTION_COOLDOWN", "8") or 8)
        url = f"rtsp://{conf.get('CAM_USER', 'admin')}:{conf.get('CAM_PASSWORD', '')}@127.0.0.1:{RTSP_PORT}/{self.cam_id}"
        w, h = 64, 36
        n = w * h
        vf = f"fps={fps},scale={w}:{h},format=gray"
        cmd = ["ffmpeg", "-nostats", "-hide_banner", "-loglevel", "error",
               "-rtsp_transport", "tcp", "-i", url, "-an", "-vf", vf,
               "-f", "rawvideo", "-flush_packets", "1", "pipe:1"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        log.info("[%s] Motion analysis running (threshold %.1f, %s fps)",
                 self.cam_id, thr, fps)
        mask = zone_mask(cam["motion_zones"], w, h)   # nur diese Pixel zählen
        prev, consec, last_over, warm = None, 0, 0.0, 6  # erste Frames (Startruckler) verwerfen
        checked = time.time()
        try:
            while True:
                buf = read_exact(self.proc.stdout, n)
                if not buf:
                    break
                if prev is not None and warm > 0:
                    warm -= 1
                elif prev is not None:
                    if mask:
                        score = sum(abs(buf[i] - prev[i]) for i in mask) / len(mask)
                    else:
                        score = sum(abs(a - b) for a, b in zip(buf, prev)) / n
                    now = time.time()
                    if score > thr:
                        consec += 1
                        last_over = now
                    else:
                        consec = 0
                    if not self.state and consec >= 2:        # Onset-Entprellung
                        self.set_state(True)
                    elif self.state and now - last_over > cooldown:
                        self.set_state(False)
                if self.state:
                    self.mark()
                prev = buf
                if self.sub_count() == 0 and time.time() - checked > 5:
                    checked = time.time()   # ohne HA-Abo nur für die Aufzeichnung weiterlaufen
                    if not recording_wants_motion(load_conf(CONF)):
                        break
        finally:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                pass
            self.proc = None


# --------------------------------------------------------------------------- ONVIF
class Onvif:
    """Ein ONVIF-Gerät = eine Kamera."""

    def __init__(self, cam):
        self.cam = cam
        self.id = cam["id"]                 # go2rtc-Stream und Profil-Token
        self.port = cam["port"]
        self.name = re.sub(r"[^A-Za-z0-9_.-]+", "_", cam["name"]) or "OnviPi"
        self.w, self.h = output_size(cam)   # bei Hochformat getauscht
        self.fps = cam["fps"]
        # Profile: Hauptstream, optional der Zweitstream in kleiner Auflösung
        self.profiles = [(self.id, self.w, self.h, "onvipi", "vec0")]
        if cam.get("substream"):
            sw, sh = sub_size(cam)
            self.profiles.append((sub_id(cam), sw, sh, "sub", "vec1"))
        self.uuid = camera_uuid(cam)
        self.subs = {}
        self.sub_lock = threading.Lock()
        self.sub_next = 0
        self.motion = Motion(self.id, self.sub_count)

    def mac(self):
        return effective_mac(self.cam)

    # -- Subscriptions
    def sub_count(self):
        with self.sub_lock:
            return len(self.subs)

    def new_sub(self):
        with self.sub_lock:
            idx = self.sub_next
            self.sub_next += 1
            self.subs[idx] = {"seq": self.motion.seq, "need_initial": True}
        return idx

    def get_sub(self, idx):
        with self.sub_lock:
            return self.subs.setdefault(idx, {"seq": self.motion.seq, "need_initial": True})

    def del_sub(self, idx):
        with self.sub_lock:
            self.subs.pop(idx, None)

    # -- Adressen
    def ip(self):
        return primary_ip() or "127.0.0.1"

    def xaddr(self, svc):
        return f"http://{self.ip()}:{self.port}/onvif/{svc}"

    # -- Device
    def GetSystemDateAndTime(self, _):
        t = time.gmtime()
        return (
            "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
            "<tt:DateTimeType>Manual</tt:DateTimeType>"
            "<tt:DaylightSavings>false</tt:DaylightSavings>"
            "<tt:TimeZone><tt:TZ>UTC0</tt:TZ></tt:TimeZone>"
            f"<tt:UTCDateTime><tt:Time><tt:Hour>{t.tm_hour}</tt:Hour>"
            f"<tt:Minute>{t.tm_min}</tt:Minute><tt:Second>{t.tm_sec}</tt:Second></tt:Time>"
            f"<tt:Date><tt:Year>{t.tm_year}</tt:Year><tt:Month>{t.tm_mon}</tt:Month>"
            f"<tt:Day>{t.tm_mday}</tt:Day></tt:Date></tt:UTCDateTime>"
            "</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
        )

    def GetDeviceInformation(self, _):
        return (
            "<tds:GetDeviceInformationResponse>"
            "<tds:Manufacturer>Raspberry Pi</tds:Manufacturer>"
            "<tds:Model>OnviPi</tds:Model>"
            "<tds:FirmwareVersion>1.0</tds:FirmwareVersion>"
            f"<tds:SerialNumber>{self.uuid}</tds:SerialNumber>"
            "<tds:HardwareId>OnviPi-Pi4</tds:HardwareId>"
            "</tds:GetDeviceInformationResponse>"
        )

    def _svc(self, ns, svc):
        return (f"<tds:Service><tds:Namespace>{ns}</tds:Namespace>"
                f"<tds:XAddr>{self.xaddr(svc)}</tds:XAddr>"
                "<tds:Version><tt:Major>2</tt:Major><tt:Minor>5</tt:Minor></tds:Version>"
                "</tds:Service>")

    def GetServices(self, _):
        return (
            "<tds:GetServicesResponse>"
            + self._svc("http://www.onvif.org/ver10/device/wsdl", "device_service")
            + self._svc("http://www.onvif.org/ver10/media/wsdl", "media_service")
            + self._svc("http://www.onvif.org/ver10/events/wsdl", "events_service")
            + "</tds:GetServicesResponse>"
        )

    def GetCapabilities(self, _):
        return (
            "<tds:GetCapabilitiesResponse><tds:Capabilities>"
            f"<tt:Device><tt:XAddr>{self.xaddr('device_service')}</tt:XAddr></tt:Device>"
            f"<tt:Events><tt:XAddr>{self.xaddr('events_service')}</tt:XAddr>"
            "<tt:WSSubscriptionPolicySupport>false</tt:WSSubscriptionPolicySupport>"
            "<tt:WSPullPointSupport>true</tt:WSPullPointSupport>"
            "<tt:WSPausableSubscriptionManagerInterfaceSupport>false"
            "</tt:WSPausableSubscriptionManagerInterfaceSupport></tt:Events>"
            f"<tt:Media><tt:XAddr>{self.xaddr('media_service')}</tt:XAddr>"
            "<tt:StreamingCapabilities><tt:RTPMulticast>false</tt:RTPMulticast>"
            "<tt:RTP_TCP>true</tt:RTP_TCP><tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP>"
            "</tt:StreamingCapabilities></tt:Media>"
            "</tds:Capabilities></tds:GetCapabilitiesResponse>"
        )

    def GetNetworkInterfaces(self, _):
        # HA nimmt die MAC als eindeutige ID des Geräts; ohne Antwort bricht der Flow ab
        mac = self.mac()
        if not mac:
            raise NotImplementedError
        name = primary_interface() or "eth0"
        return (
            "<tds:GetNetworkInterfacesResponse>"
            f'<tds:NetworkInterfaces token="{name}">'
            "<tt:Enabled>true</tt:Enabled>"
            f"<tt:Info><tt:Name>{name}</tt:Name><tt:HwAddress>{mac}</tt:HwAddress></tt:Info>"
            "</tds:NetworkInterfaces></tds:GetNetworkInterfacesResponse>"
        )

    def GetScopes(self, _):
        scopes = [
            "onvif://www.onvif.org/Profile/Streaming",
            "onvif://www.onvif.org/type/video_encoder",
            f"onvif://www.onvif.org/name/{self.name}",
            "onvif://www.onvif.org/hardware/RaspberryPi",
        ]
        mac = self.mac()
        if mac:
            scopes.append(f"onvif://www.onvif.org/MAC/{mac}")
        items = "".join(
            "<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef>"
            f"<tt:ScopeItem>{s}</tt:ScopeItem></tds:Scopes>" for s in scopes)
        return f"<tds:GetScopesResponse>{items}</tds:GetScopesResponse>"

    def SystemReboot(self, _):
        log.info("Reboot requested via ONVIF")
        threading.Timer(1.5, privileged, args=("reboot",)).start()  # erst antworten
        return ("<tds:SystemRebootResponse><tds:Message>Rebooting now</tds:Message>"
                "</tds:SystemRebootResponse>")

    def SetSystemDateAndTime(self, body):
        """HA-Taste "Set system date and time": Uhr auf die Zeit des Clients stellen.

        Der Pi holt die Zeit selbst per NTP; die Hilfsroutine stellt die Uhr deshalb nur,
        solange keine NTP-Synchronisation besteht (z. B. Netz ohne Internet).
        """
        utc = body.partition("UTCDateTime")[2]
        try:
            y, mo, d, h, mi, sec = (int(tag_value(utc, k)) for k in
                                    ("Year", "Month", "Day", "Hour", "Minute", "Second"))
            epoch = calendar.timegm((y, mo, d, h, mi, sec))
        except (TypeError, ValueError):
            epoch = None  # z. B. DateTimeType=NTP ohne Zeitangabe – nichts zu tun
        if epoch:
            privileged("set-time", str(epoch))
        return "<tds:SetSystemDateAndTimeResponse/>"

    def GetServiceCapabilities(self, _):
        return ("<tds:GetServiceCapabilitiesResponse><tds:Capabilities>"
                "<tds:Network IPFilter=\"false\" DynDNS=\"false\"/>"
                "</tds:Capabilities></tds:GetServiceCapabilitiesResponse>")

    # -- Media
    def GetVideoSources(self, _):
        return (
            "<trt:GetVideoSourcesResponse>"
            f"<trt:VideoSources token=\"vs0\"><tt:Framerate>{self.fps}</tt:Framerate>"
            f"<tt:Resolution><tt:Width>{self.w}</tt:Width><tt:Height>{self.h}</tt:Height>"
            "</tt:Resolution></trt:VideoSources></trt:GetVideoSourcesResponse>"
        )

    def _profile(self, profile):
        token, w, h, name, vec = profile
        return (
            f"<trt:Profiles token=\"{token}\" fixed=\"true\"><tt:Name>{name}</tt:Name>"
            "<tt:VideoSourceConfiguration token=\"vsc0\"><tt:Name>vsc0</tt:Name>"
            f"<tt:UseCount>{len(self.profiles)}</tt:UseCount><tt:SourceToken>vs0</tt:SourceToken>"
            f"<tt:Bounds x=\"0\" y=\"0\" width=\"{self.w}\" height=\"{self.h}\"/>"
            "</tt:VideoSourceConfiguration>"
            f"<tt:VideoEncoderConfiguration token=\"{vec}\"><tt:Name>{vec}</tt:Name>"
            "<tt:UseCount>1</tt:UseCount><tt:Encoding>H264</tt:Encoding>"
            f"<tt:Resolution><tt:Width>{w}</tt:Width><tt:Height>{h}</tt:Height></tt:Resolution>"
            "<tt:Quality>5</tt:Quality>"
            f"<tt:RateControl><tt:FrameRateLimit>{self.fps}</tt:FrameRateLimit>"
            "<tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>2500</tt:BitrateLimit>"
            "</tt:RateControl>"
            "<tt:H264><tt:GovLength>30</tt:GovLength><tt:H264Profile>Main</tt:H264Profile></tt:H264>"
            "<tt:SessionTimeout>PT60S</tt:SessionTimeout>"
            "</tt:VideoEncoderConfiguration></trt:Profiles>"
        )

    def _requested(self, body):
        """Profil aus <ProfileToken> der Anfrage, sonst das Hauptprofil."""
        token = tag_value(body or "", "ProfileToken")
        return next((p for p in self.profiles if p[0] == token), self.profiles[0])

    def GetProfiles(self, _):
        return ("<trt:GetProfilesResponse>" + "".join(self._profile(p) for p in self.profiles)
                + "</trt:GetProfilesResponse>")

    def GetProfile(self, body):
        p = (self._profile(self._requested(body)).replace("<trt:Profiles ", "<trt:Profile ")
             .replace("</trt:Profiles>", "</trt:Profile>"))
        return f"<trt:GetProfileResponse>{p}</trt:GetProfileResponse>"

    def _media_uri(self, tag, uri):
        return (f"<trt:{tag}Response><trt:MediaUri><tt:Uri>{uri}</tt:Uri>"
                "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
                "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
                f"<tt:Timeout>PT60S</tt:Timeout></trt:MediaUri></trt:{tag}Response>")

    def GetStreamUri(self, body):
        stream = self._requested(body)[0]
        return self._media_uri("GetStreamUri", f"rtsp://{self.ip()}:{RTSP_PORT}/{stream}")

    def GetSnapshotUri(self, _):
        return self._media_uri("GetSnapshotUri",
                               f"http://{self.ip()}:{GO2RTC_API}/api/frame.jpeg?src={self.id}")

    def GetServiceCapabilitiesMedia(self, _):
        return ("<trt:GetServiceCapabilitiesResponse><trt:Capabilities>"
                f"<trt:ProfileCapabilities MaximumNumberOfProfiles=\"{len(self.profiles)}\"/>"
                "<trt:StreamingCapabilities RTP_RTSP_TCP=\"true\"/>"
                "</trt:Capabilities></trt:GetServiceCapabilitiesResponse>")

    # -- Events
    def GetServiceCapabilitiesEvents(self, _):
        return ("<tev:GetServiceCapabilitiesResponse><tev:Capabilities "
                "WSSubscriptionPolicySupport=\"false\" WSPullPointSupport=\"true\" "
                "WSPausableSubscriptionManagerInterfaceSupport=\"false\" "
                "MaxNotificationProducers=\"1\" MaxPullPoints=\"2\" "
                "PersistentNotificationStorage=\"false\"/>"
                "</tev:GetServiceCapabilitiesResponse>")

    def GetEventProperties(self, _):
        return (
            "<tev:GetEventPropertiesResponse>"
            "<tev:TopicNamespaceLocation>http://www.onvif.org/onvif/ver10/topics/topicns.xml"
            "</tev:TopicNamespaceLocation>"
            "<wsnt:FixedTopicSet>true</wsnt:FixedTopicSet>"
            "<wstop:TopicSet><tns1:RuleEngine><CellMotionDetector>"
            "<Motion wstop:topic=\"true\"><tt:MessageDescription IsProperty=\"true\">"
            "<tt:Source><tt:SimpleItemDescription Name=\"VideoSourceConfigurationToken\" "
            "Type=\"tt:ReferenceToken\"/></tt:Source>"
            "<tt:Data><tt:SimpleItemDescription Name=\"IsMotion\" Type=\"xsd:boolean\"/></tt:Data>"
            "</tt:MessageDescription></Motion></CellMotionDetector></tns1:RuleEngine></wstop:TopicSet>"
            "<wsnt:TopicExpressionDialect>"
            "http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet</wsnt:TopicExpressionDialect>"
            "<wsnt:TopicExpressionDialect>"
            "http://docs.oasis-open.org/wsn/t-1/TopicExpression/Concrete</wsnt:TopicExpressionDialect>"
            "<tev:MessageContentFilterDialect>"
            "http://www.onvif.org/ver10/tev/messageContentFilter/ItemFilter"
            "</tev:MessageContentFilterDialect>"
            "</tev:GetEventPropertiesResponse>"
        )

    def CreatePullPointSubscription(self, _):
        idx = self.new_sub()
        url = f"{self.xaddr('subscription')}?idx={idx}"
        return (
            "<tev:CreatePullPointSubscriptionResponse>"
            f"<tev:SubscriptionReference><wsa:Address>{url}</wsa:Address>"
            "</tev:SubscriptionReference>"
            f"<wsnt:CurrentTime>{iso()}</wsnt:CurrentTime>"
            f"<wsnt:TerminationTime>{iso(60)}</wsnt:TerminationTime>"
            "</tev:CreatePullPointSubscriptionResponse>"
        )

    def _notification(self, state, op):
        val = "true" if state else "false"
        return (
            "<wsnt:NotificationMessage>"
            "<wsnt:Topic Dialect=\"http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet\">"
            "tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>"
            "<wsnt:Message>"
            f"<tt:Message UtcTime=\"{iso()}\" PropertyOperation=\"{op}\">"
            "<tt:Source><tt:SimpleItem Name=\"VideoSourceConfigurationToken\" Value=\"vsc0\"/>"
            "<tt:SimpleItem Name=\"Rule\" Value=\"MotionDetector\"/></tt:Source>"
            f"<tt:Data><tt:SimpleItem Name=\"IsMotion\" Value=\"{val}\"/></tt:Data>"
            "</tt:Message></wsnt:Message></wsnt:NotificationMessage>"
        )

    def PullMessages(self, body, idx):
        sub = self.get_sub(idx)
        msgs = ""
        if sub.get("need_initial"):
            sub["need_initial"] = False
            sub["seq"] = self.motion.seq
            msgs = self._notification(self.motion.state, "Initialized")
        else:
            m = re.search(r"PT(\d+)S", body)
            wait = min(int(m.group(1)) if m else 20, 25)
            seq, state = self.motion.wait_change(sub["seq"], wait)
            if seq != sub["seq"]:
                sub["seq"] = seq
                msgs = self._notification(state, "Changed")
        return (f"<tev:PullMessagesResponse><tev:CurrentTime>{iso()}</tev:CurrentTime>"
                f"<tev:TerminationTime>{iso(60)}</tev:TerminationTime>{msgs}"
                "</tev:PullMessagesResponse>")

    def Renew(self, body, idx):
        return (f"<wsnt:RenewResponse><wsnt:TerminationTime>{iso(60)}</wsnt:TerminationTime>"
                f"<wsnt:CurrentTime>{iso()}</wsnt:CurrentTime></wsnt:RenewResponse>")

    def Unsubscribe(self, body, idx):
        self.del_sub(idx)
        return "<wsnt:UnsubscribeResponse/>"

    def SetSynchronizationPoint(self, body, idx):
        self.get_sub(idx)["need_initial"] = True
        return "<tev:SetSynchronizationPointResponse/>"


DEVICE_OPS = {"GetSystemDateAndTime", "GetDeviceInformation", "GetServices",
              "GetCapabilities", "GetScopes", "GetServiceCapabilities",
              "GetNetworkInterfaces", "SystemReboot", "SetSystemDateAndTime"}
MEDIA_OPS = {"GetVideoSources", "GetProfiles", "GetProfile", "GetStreamUri",
             "GetSnapshotUri", "GetServiceCapabilities"}
EVENTS_OPS = {"GetServiceCapabilities", "GetEventProperties", "CreatePullPointSubscription",
              "PullMessages", "Renew", "Unsubscribe", "SetSynchronizationPoint"}
IDX_OPS = {"PullMessages", "Renew", "Unsubscribe", "SetSynchronizationPoint"}
OP_RE = re.compile(r"<(?:[\w.-]+:)?(\w+)[ />]")


def privileged(*args):
    """Der Dienst läuft als 'onvipi'; Neustart/Uhrzeit erledigt onvipi_priv.py per sudo."""
    try:
        r = subprocess.run(["sudo", "-n", "/usr/bin/python3", PRIV, *args],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            log.warning("onvipi_priv %s failed: %s", args[0], r.stderr.strip())
    except (OSError, subprocess.SubprocessError) as ex:
        log.warning("onvipi_priv %s failed: %s", args[0], ex)


def find_op(body, allowed):
    for m in OP_RE.finditer(body):
        if m.group(1) in allowed:
            return m.group(1)
    return None


# Laut ONVIF-Core auch ohne Anmeldung erlaubt (Clients fragen damit u. a. die Uhrzeit ab)
PRE_AUTH_OPS = {"GetSystemDateAndTime", "GetServices", "GetServiceCapabilities",
                "GetCapabilities"}


def authorized(body):
    """WS-Security UsernameToken (PasswordDigest oder PasswordText) gegen onvipi.conf prüfen.

    Wichtig, weil HA die hier eingegebenen Zugangsdaten auch für RTSP verwendet –
    ohne Prüfung würde die Einrichtung mit falschem Passwort "klappen", das Bild aber nicht.
    """
    conf = load_conf(CONF)
    user, pw = conf.get("CAM_USER", "admin"), conf.get("CAM_PASSWORD", "")
    if not pw:
        return True
    got_user = tag_value(body, "Username")
    got_pw = tag_value(body, "Password")
    if got_user is None or got_pw is None or not hmac.compare_digest(got_user, user):
        return False
    if "PasswordDigest" in body:
        try:
            nonce = base64.b64decode(tag_value(body, "Nonce") or "")
        except ValueError:
            return False
        created = tag_value(body, "Created") or ""
        expected = base64.b64encode(
            hashlib.sha1(nonce + created.encode() + pw.encode()).digest()).decode()
        return hmac.compare_digest(got_pw, expected)
    return hmac.compare_digest(got_pw, pw)


def tag_value(xml, name):
    m = re.search(r"<(?:[\w.-]+:)?%s\b[^>]*>([^<]*)</" % name, xml)
    return m.group(1).strip() if m else None


def fault(reason):
    return ("<s:Fault><s:Code><s:Value>s:Receiver</s:Value></s:Code>"
            f"<s:Reason><s:Text xml:lang=\"en\">{reason}</s:Text></s:Reason></s:Fault>")


def serve(onvif):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "OnviPi-ONVIF"

        def log_message(self, fmt, *args):
            log.debug("http: " + fmt, *args)

        def _send(self, body, code=200):
            data = ENVELOPE.format(body=body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _resolve(self, path, body):
            if path.endswith("/media_service"):
                op = find_op(body, MEDIA_OPS)
                fn = ({"GetServiceCapabilities": onvif.GetServiceCapabilitiesMedia}.get(op)
                      or (getattr(onvif, op, None) if op else None))
                return op, fn, False
            if path.endswith("/events_service") or "/subscription" in path:
                op = find_op(body, EVENTS_OPS)
                fn = {"GetServiceCapabilities": onvif.GetServiceCapabilitiesEvents,
                      "GetEventProperties": onvif.GetEventProperties,
                      "CreatePullPointSubscription": onvif.CreatePullPointSubscription,
                      "PullMessages": onvif.PullMessages, "Renew": onvif.Renew,
                      "Unsubscribe": onvif.Unsubscribe,
                      "SetSynchronizationPoint": onvif.SetSynchronizationPoint}.get(op)
                return op, fn, op in IDX_OPS
            op = find_op(body, DEVICE_OPS)   # device_service (Standard)
            return op, (getattr(onvif, op, None) if op else None), False

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            parts = urllib.parse.urlparse(self.path)
            op, fn, needs_idx = self._resolve(parts.path, body)
            if not fn:
                log.warning("Unknown ONVIF operation on %s: %r", parts.path, body[:120])
                # ONVIF-Standardtext: Clients (u. a. HA) erkennen daran "optional, fehlt"
                return self._send(fault("Optional Action Not Implemented"), 500)
            if op not in PRE_AUTH_OPS and not authorized(body):
                log.warning("ONVIF %s from %s rejected: wrong credentials", op,
                            self.client_address[0])
                return self._send(fault("Sender not Authorized"), 400)
            try:
                if needs_idx:
                    idx = int(urllib.parse.parse_qs(parts.query).get("idx", ["0"])[0])
                    out = fn(body, idx)
                else:
                    out = fn(body)
                log.debug("ONVIF %s %s", parts.path.rsplit("/", 1)[-1], op)
                return self._send(out)
            except NotImplementedError:
                return self._send(fault("Optional Action Not Implemented"), 500)
            except Exception:
                log.exception("Error in %s", op)
                return self._send(fault("Internal error"), 500)

        def do_GET(self):
            # Home Assistant verlinkt auf der Geräteseite ("Visit") http://<ip>:<onvif-port>
            # -> zur Einstellungsseite weiterleiten.
            host = (self.headers.get("Host") or onvif.ip()).rsplit(":", 1)[0]
            self.send_response(302)
            self.send_header("Location", f"http://{host}/")
            self.send_header("Content-Length", "0")
            self.end_headers()

    http.server.ThreadingHTTPServer.allow_reuse_address = True
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", onvif.port), Handler)
    log.info("ONVIF device '%s' on :%d  (XAddr %s)", onvif.name, onvif.port,
             onvif.xaddr("device_service"))
    srv.serve_forever()


def main():
    cams = [c for c in load_cameras() if c["enabled"]]
    if not cams:
        log.warning("No camera enabled – waiting (the service restarts when one is set up)")
    ports = set()
    for cam in cams:
        if cam["port"] in ports:
            log.error("Port %d is used twice – camera '%s' skipped", cam["port"], cam["name"])
            continue
        ports.add(cam["port"])
        threading.Thread(target=serve, args=(Onvif(cam),), daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"),
                        format="%(name)s: %(message)s")
    main()
