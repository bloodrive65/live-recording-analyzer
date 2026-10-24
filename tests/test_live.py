"""Live advisor test: feeds a simulated recording (drive stall, encoder overload, late audio, file stops growing)
through the live advisor and checks that each expected alert appears, with the right severity.

Uses a throwaway APPDATA folder with a made-up OBS log, so a real OBS log is never written to.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as fx  # noqa: E402


class FakeMon:
    obs_status = "connected"
    latest = None


class LiveAlerts(unittest.TestCase):
    def setUp(self):
        self.ws = fx.Workspace()
        self.addCleanup(self.ws.cleanup)
        env = mock.patch.dict(os.environ, {"APPDATA": self.ws.appdata, "LOCALAPPDATA": self.ws.appdata})
        env.start()
        self.addCleanup(env.stop)

    def simulate(self):
        from analyzer import live
        mon = FakeMon()
        adv = live.LiveAdvisor(mon)
        adv.preflight(announce=False)
        adv._open_log(seek_end=True)
        logp = self.ws.log
        self.assertTrue(os.path.abspath(logp).startswith(os.path.abspath(self.ws.dir)))

        def logline(t):
            with open(logp, "a", encoding="utf-8") as f:
                f.write(datetime.now().strftime("%H:%M:%S.000") + ": " + t + "\n")
        t0 = datetime.now()
        es = rb = 0
        for i in range(40):
            rec = 2 <= i <= 34
            s = {"ts": (t0 + timedelta(seconds=i)).isoformat(timespec="milliseconds"), "cpu": 20, "cpu_core_max": 50, "ram": 50,
                 "gpu_3d": 40, "gpu_enc": 70, "disks": {"Z:": {"busy": 10, "write_ms": 8}},
                 "procs": {"cpu": [["obs64.exe", 8]], "io": []}}
            o = {"fps": 60, "render_ms": 3, "render_skip": 0, "enc_skip": es, "rec": rec, "rec_paused": False,
                 "rec_ms": max(0, i - 2) * 1000, "rec_mbs": 19, "disk_free_mb": 900000}
            if rec and not (24 <= i <= 31):
                rb += 20_000_000          # file grows, except for 8 s near the end
            o["rec_bytes"] = rb
            if i == 2:
                logline("[ffmpeg muxer: 'adv_file_output'] Writing file 'Z:/Recordings/test.mkv'...")
            if i in (6, 7, 8):
                s["disks"]["Z:"] = {"busy": 100, "write_ms": 1300, "queue": 8}
                es += 4
                o["enc_skip"], o["d_enc"] = es, 4
            if 12 <= i <= 22:
                s["gpu_enc"] = 99
            if i == 14:
                logline("adding 64 milliseconds of audio buffering, total audio buffering is now 277 milliseconds "
                        "(source: Video Capture Device)")
            if i == 34:
                logline("Video stopped, number of skipped frames due to encoding lag: 12/2000 (0.6%)")
            s["obs"], s["type"] = o, "sample"
            mon.latest = s
            s2 = dict(s)
            s2["_dt"] = datetime.fromisoformat(s["ts"])
            adv.hist.append(s2)
            adv._on_sample(s2)
            adv._tail_log()
        video = fx.cached_video("test.mkv", seconds=30, gaps=True)
        for x in live.quick_file_check(video, adv.cfg):
            adv.post(x)
        return [a for a in adv.drain() if a.phase != "before"]

    def test_expected_alerts_appear(self):
        alerts = self.simulate()
        got = {(a.severity, a.title) for a in alerts}
        text = "\n".join(f"{s} | {t}" for s, t in sorted(got))

        def has(sev, part):
            self.assertTrue(any(s == sev and part in t for s, t in got), f"missing {sev} alert containing {part!r}:\n{text}")
        has("warning", "Recording drive Z: is stalling")
        has("warning", "Video encoder is at its limit")
        has("warning", "OBS added 64 ms of audio buffering")
        has("critical", "The recording file has stopped growing")
        has("warning", "OBS final count: 12 of 2,000 frames skipped")
        has("warning", "8 frame(s) already missing")

    def test_no_alarm_for_a_healthy_recording(self):
        """Quiet samples must not produce problem alerts (no false alarms while gaming)."""
        from analyzer import live
        mon = FakeMon()
        adv = live.LiveAdvisor(mon)
        adv.preflight(announce=False)
        adv._open_log(seek_end=True)
        t0 = datetime.now()
        rb = 0
        for i in range(40):
            rb += 20_000_000
            s = {"type": "sample", "ts": (t0 + timedelta(seconds=i)).isoformat(timespec="milliseconds"), "cpu": 20,
                 "cpu_core_max": 50, "ram": 50, "gpu_3d": 40, "gpu_enc": 60, "disks": {"Z:": {"busy": 10, "write_ms": 8}},
                 "procs": {"cpu": [], "io": []},
                 "obs": {"fps": 60, "render_ms": 3, "render_skip": 0, "enc_skip": 0, "rec": True, "rec_paused": False,
                         "rec_ms": i * 1000, "rec_bytes": rb, "rec_mbs": 19, "disk_free_mb": 900000}}
            mon.latest = s
            s2 = dict(s)
            s2["_dt"] = datetime.fromisoformat(s["ts"])
            adv.hist.append(s2)
            adv._on_sample(s2)
        bad = [a for a in adv.drain() if a.phase != "before" and a.severity in ("warning", "critical")]
        self.assertEqual(bad, [], [a.title for a in bad])


if __name__ == "__main__":
    unittest.main(verbosity=2)
