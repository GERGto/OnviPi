"""Tests for the parts of OnviPi that need no Raspberry Pi: camera list, stream configuration,
privacy masks, recording ring buffer, motion clips and update decisions.

    python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import time
import unittest

TMP = tempfile.mkdtemp(prefix="onvipi-test-")
os.environ.update(ONVIPI_CONF_DIR=TMP, ONVIPI_RUN_DIR=os.path.join(TMP, "run"),
                  ONVIPI_REC_LOCAL=os.path.join(TMP, "rec"), ONVIPI_REC_MOUNT=os.path.join(TMP, "usb"),
                  ONVIPI_REC_STATUS=os.path.join(TMP, "rec-status.json"),
                  ONVIPI_UPDATE_STATUS=os.path.join(TMP, "upd-status.json"),
                  ONVIPI_UPDATE_STATE=os.path.join(TMP, "upd-state.json"))
os.makedirs(os.environ["ONVIPI_RUN_DIR"], exist_ok=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "onvipi", "pi"))

import onvipi_camera as camera  # noqa: E402
import onvipi_onvif as onvif  # noqa: E402
import onvipi_record as record  # noqa: E402
import onvipi_stream as stream  # noqa: E402
import onvipi_update as update  # noqa: E402
from onvipilib import CONF, save_conf  # noqa: E402

MB = 1 << 20
CONF_DATA = {"CAM_USER": "admin", "CAM_PASSWORD": "secret123", "HW_ENCODE": "0"}


def cam(**extra):
    return camera.normalize(dict({"id": "onvipi", "name": "Garden", "device": "/dev/video0",
                                  "size": "1280x720", "fps": 30}, **extra))


class CameraList(unittest.TestCase):
    def test_defaults_for_old_entries(self):
        c = cam()
        self.assertEqual((c["audio"], c["substream"], c["privacy"], c["motion_zones"]), (False, False, [], []))

    def test_hostile_values_are_neutralised(self):
        c = camera.normalize({"id": "x#exec", "name": "<b>$(reboot)`", "format": "mjpeg#video=copy",
                              "device": "/dev/video0#raw=-f null /etc/passwd", "size": "1x1;rm"})
        self.assertEqual((c["id"], c["device"], c["format"], c["size"]), ("onvipi", "", "mjpeg", "1280x720"))
        self.assertNotRegex(c["name"], r"[<>$`]")
        self.assertEqual(camera.normalize({"device": "/dev/../etc/shadow"})["device"], "")

    def test_usual_device_paths_pass(self):
        for dev in ("/dev/v4l/by-id/usb-046d_HD_Pro_Webcam_C920_1A2B3C4D-video-index0",
                    "/dev/v4l/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.2:1.0-video-index0"):
            self.assertEqual(camera.normalize({"device": dev})["device"], dev)

    def test_zones_are_cleaned(self):
        zones = camera.clean_zones([{"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}, {"x": 0.9, "y": 0.9, "w": 0.5, "h": 0.5},
                                    {"x": "a"}, {"x": 0, "y": 0, "w": 0.001, "h": 0.5}])
        self.assertEqual(len(zones), 2)
        self.assertEqual(zones[1]["w"], 0.1)

    def test_second_camera_gets_own_identity(self):
        first = cam()
        second = camera.new_camera([first], {"dev": "/dev/video2", "label": "B",
                                             "formats": {"mjpeg": {"640x480": [30]}}}, CONF_DATA)
        self.assertEqual((second["id"], second["port"]), ("cam2", 8001))
        self.assertEqual(int(second["mac"][:2], 16) & 3, 2)   # locally administered address
        self.assertNotEqual(camera.camera_uuid(first), camera.camera_uuid(second))


class Streams(unittest.TestCase):
    def test_go2rtc_configuration(self):
        camera.microphone_of = lambda dev: "plughw:CARD=C920,DEV=0" if dev == "/dev/video2" else None
        plain = cam()
        full = cam(id="cam2", device="/dev/video2", size="1920x1080", fps=15, rotate=90, audio=True,
                   substream=True, privacy=[{"x": 0.5, "y": 0, "w": 0.5, "h": 0.25}])
        cfg = camera.render(CONF_DATA, [plain, full])
        self.assertEqual(cfg["api"]["listen"], "127.0.0.1:1984")       # never open to the network
        self.assertTrue(cfg["streams"]["onvipi"].startswith("ffmpeg:device?video=/dev/video0"))
        self.assertEqual(len(cfg["streams"]["cam2"]), 2)               # masked video + sound
        self.assertEqual(cfg["streams"]["cam2_sub"], "ffmpeg:cam2#video=h264#width=640#height=1138#audio=copy")

    def test_privacy_mask_reaches_the_edge(self):
        self.assertEqual(stream.mask_pixels({"x": 0.6667, "y": 0, "w": 0.3333, "h": 0.25}, 1280, 720),
                         (852, 0, 1280, 180))
        self.assertEqual(stream.mask_pixels({"x": 0.004, "y": 0.5, "w": 0.99, "h": 0.5}, 720, 1280),
                         (0, 640, 720, 1280))
        x0, y0, x1, y1 = stream.mask_pixels({"x": 0.333, "y": 0.333, "w": 0.2, "h": 0.2}, 1281, 721)
        self.assertEqual((x0 % 2, y0 % 2, x1 % 2, y1 % 2), (0, 0, 0, 0))

    def test_video_command_order(self):
        c = cam(rotate=90, privacy=[{"x": 0, "y": 0, "w": 0.5, "h": 0.5}])
        vf = stream.video_filters(c)
        self.assertTrue(vf[0].startswith("transpose") and vf[1].startswith("drawbox") and vf[2].startswith("drawtext"))


class Onvif(unittest.TestCase):
    def test_second_profile_for_the_small_stream(self):
        o = onvif.Onvif(cam(id="cam2", rotate=90, substream=True, port=18555))
        self.assertEqual(o.GetProfiles("").count("<trt:Profiles "), 2)
        self.assertIn(":8554/cam2_sub<", o.GetStreamUri("<ProfileToken>cam2_sub</ProfileToken>"))
        self.assertIn(":18555/snapshot.jpg<", o.GetSnapshotUri(""))

    def test_motion_mask(self):
        mask = onvif.zone_mask([{"x": 0, "y": 0.5, "w": 0.5, "h": 0.5}], 64, 36)
        self.assertEqual((len(mask), min(mask), max(mask)), (32 * 18, 18 * 64, 35 * 64 + 31))
        self.assertIsNone(onvif.zone_mask([], 64, 36))

    def test_events_are_logged(self):
        m = onvif.Motion.__new__(onvif.Motion)   # without the analysis thread
        m.events = os.path.join(TMP, "run", "motion-t.log")
        m.peak, m.last_event = 12.3, None
        m.log_event(1000.0, 1012.5)
        self.assertEqual(record.read_events(m.events), [(1000.0, 1012.5, 12.3)])
        self.assertEqual(m.last_event, (1000.0, 1012.5))

    def test_digest_and_basic_login(self):
        save_conf(CONF, CONF_DATA)
        import base64
        ok = "Basic " + base64.b64encode(b"admin:secret123").decode()
        self.assertTrue(onvif.http_authorized(ok, "GET"))
        self.assertFalse(onvif.http_authorized("Basic " + base64.b64encode(b"admin:nope").decode(), "GET"))
        self.assertFalse(onvif.http_authorized(None, "GET"))
        self.assertFalse(onvif.nonce_valid("1-abc"))
        self.assertTrue(onvif.nonce_valid(onvif.new_nonce()))


class Recording(unittest.TestCase):
    def setUp(self):
        self.disk = {"total": 1000 * MB, "other": 0}
        record.mount(record.LOCAL)
        for name in ("a", "b"):
            for folder in (record.camera_dir(name), record.live_dir(name)):
                os.makedirs(folder, exist_ok=True)
                for f in os.listdir(folder):
                    if f.endswith((".mp4", ".log")):
                        os.remove(os.path.join(folder, f))
        self._usage, self._reserve, self._boot = record.usage, record.reserve, record.system_boot
        record.usage = lambda: (self.disk["total"], max(0, self.disk["total"] - self.disk["other"] - self.used()))
        record.reserve = lambda total: 200 * MB
        record.system_boot = lambda: time.time() - 86400
        self.now = time.time()
        self.rec = record.Recorder()

    def tearDown(self):
        record.usage, record.reserve, record.system_boot = self._usage, self._reserve, self._boot

    def used(self):
        return sum(size for c in ("a", "b") for _, _, size in record.list_files(c))

    def make(self, cam_id, age, size_mb=1, live=False):
        folder = record.live_dir(cam_id) if live else record.camera_dir(cam_id)
        name = time.strftime("%Y%m%d-%H%M%S.mp4", time.localtime(self.now - age))
        with open(os.path.join(folder, name), "wb") as f:
            f.truncate(size_mb * MB)
        return name

    def names(self, cam_id, live=False):
        return [n for _, n, _ in record.list_files(cam_id, live)]

    def test_days_reserve_and_limit(self):
        old = self.make("a", 80 * 3600, 10)
        keep = [self.make("a", h * 3600, 10) for h in (40, 20, 1)]
        self.rec.prune(days=2)
        self.assertEqual(self.names("a"), keep)
        self.assertNotIn(old, self.names("a"))
        for h in range(30, 0, -1):
            self.make("b", (h + 0.5) * 3600, 30)
        self.rec.last_prune = 0
        self.rec.prune(days=30, force=True)
        self.assertGreaterEqual(record.usage()[1], 200 * MB)
        self.assertIn(keep[-1], self.names("a"))            # the segment being written stays
        self.rec.last_prune = 0
        self.rec.prune(days=30, limit=300 * MB, force=True)
        self.assertLessEqual(self.used(), 300 * MB)

    def test_pause_instead_of_overfilling(self):
        self.make("a", 3600, 10), self.make("a", 60, 10)
        foreign = os.path.join(record.camera_dir("a"), "holiday.mov")
        open(foreign, "wb").close()
        self.disk["other"] = 950 * MB
        self.rec.prune(days=30, force=True)
        self.assertTrue(os.path.exists(foreign))             # other people's files are never touched
        self.assertFalse(self.rec.guard())
        self.disk["other"] = 800 * MB
        self.assertFalse(self.rec.guard())                   # still below the full reserve
        self.disk["other"] = 100 * MB
        self.assertTrue(self.rec.guard())
        os.remove(foreign)

    def test_motion_clips_only_with_a_reason(self):
        c = cam(id="a")
        quiet = self.make("a", 400, live=True)
        moved = self.make("a", 340, live=True)               # movement in this minute
        lead = self.make("a", 280, live=True)                # movement starts just after it ends
        quiet2 = self.make("a", 160, live=True)
        current = self.make("a", 30, live=True)              # still being written

        class Running:
            def poll(self):
                return None
        self.rec.procs["a"] = Running()
        self.rec.motion["a"] = [self.now - 320]              # seen live through the marker
        with open(os.path.join(os.environ["ONVIPI_RUN_DIR"], "motion-a.log"), "w") as f:
            f.write(f"{self.now - 215:.1f} {self.now - 205:.1f} 9.0\n")   # from the event log
        self.rec.settle([c])
        self.assertEqual(self.names("a"), [moved, lead])     # only these appear in the gallery
        self.assertEqual(self.names("a", live=True), [current])
        self.assertNotIn(quiet, self.names("a") + self.names("a", live=True))
        self.assertNotIn(quiet2, self.names("a") + self.names("a", live=True))
        self.rec.sync_events([c])
        self.assertEqual(len(record.stored_events("a")), 1)  # the gallery can show why
        self.rec.sync_events([c])
        self.assertEqual(len(record.stored_events("a")), 1)  # not copied twice

    def test_unjudgeable_segments_are_kept(self):
        before_boot = self.make("b", 2 * 86400, live=True)
        no_detector = self.make("b", 300, live=True)
        self.make("b", 200, live=True)
        self.rec.settle([cam(id="b", motion=False)])
        self.assertIn(before_boot, self.names("b"))
        self.assertIn(no_detector, self.names("b"))

    def test_nothing_is_written_without_the_usb_storage(self):
        record.use("1234-ABCD")
        started = []
        real, record.subprocess.Popen = record.subprocess.Popen, lambda *a, **k: started.append(a)
        try:
            self.rec.ensure([cam(id="a")], CONF_DATA, "continuous")
        finally:
            record.subprocess.Popen = real
            record.use(record.LOCAL)
        self.assertEqual(started, [])

    def test_motion_clips_are_staged(self):
        self.assertIn(os.sep + ".live" + os.sep, self.rec.command(cam(id="a"), CONF_DATA, "motion")[-1])
        self.assertNotIn(".live", self.rec.command(cam(id="a"), CONF_DATA, "continuous")[-1])


class Updates(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._install, self._fetch, self._ver = update.install, update.fetch, update.current_version
        update.install = lambda: self.calls.append("install")
        update.current_version = lambda: "2.2"
        update.fetch = lambda url, timeout=30: b'{"tag_name": "v9.9", "html_url": "javascript:alert(1)"}'

    def tearDown(self):
        update.install, update.fetch, update.current_version = self._install, self._fetch, self._ver

    def test_both_switches(self):
        save_conf(CONF, {})
        update.main(["auto"])
        self.assertEqual(self.calls, ["install"])            # default: look and install
        save_conf(CONF, {"UPDATE_CHECK": "1", "UPDATE_AUTO": "0"})
        self.calls.clear()
        update.main(["auto"])
        self.assertEqual(self.calls, [])
        self.assertTrue(update.read_state()["available"])    # shown, not installed
        save_conf(CONF, {"UPDATE_CHECK": "0", "UPDATE_AUTO": "1"})
        fetched = []
        update.fetch = lambda url, timeout=30: fetched.append(url) or b"{}"
        update.main(["auto"])
        self.assertEqual((fetched, self.calls), ([], []))    # no connection at all

    def test_versions_and_links(self):
        self.assertGreater(update.version_key("2.10"), update.version_key("2.9"))
        self.assertIsNone(update.version_key("0.0.0-dev+abc"))
        self.assertEqual(update.check()["url"], "")          # only links to GitHub are passed on
        update.current_version = lambda: "dev"
        save_conf(CONF, {})
        update.main(["auto"])
        self.assertEqual(self.calls, [])                     # development builds are left alone


if __name__ == "__main__":
    unittest.main()
