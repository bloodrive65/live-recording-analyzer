"""Analysis tests: the normal path, and the failure paths that must never produce a false "all clear".

Run with:  python -m unittest discover -s tests -v
Needs ffmpeg and ffprobe on PATH (or in ./bin). Uses only throwaway files.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as fx  # noqa: E402
from analyzer import analyze, report, video_probe, weblink  # noqa: E402


def step(res, title):
    hits = [s for s in res.steps if s["title"] == title]
    assert hits, f"step {title!r} missing; got {[s['title'] for s in res.steps]}"
    return hits[-1]


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def titles(res):
    return [f.title for f in res.findings]


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = fx.Workspace()
        self.addCleanup(self.ws.cleanup)
        self.video = fx.cached_video("test.mkv", seconds=30, gaps=True)

    def run_analysis(self, video=None, deep=False):
        return analyze.run(self.ws.log, video_paths={0: video or self.video}, monitor_path=self.ws.monitor, deep=deep)


class NormalPath(Base):
    def test_full_analysis_finds_the_known_problems(self):
        res = self.run_analysis(deep=True)
        v = res.recordings[0].video
        self.assertTrue(v.ts_complete, v.ts_note)
        self.assertEqual(v.gap_frames_total, 8, "the fixture video has exactly 8 removed frames")
        self.assertEqual(len(v.gaps), 2)
        self.assertTrue(v.deep_scanned, v.deep_note)
        self.assertTrue(any("timestamp gap" in t for t in titles(res)), titles(res))
        self.assertTrue(any(i.kind == "file-gap" for i in res.incidents))
        for s in res.steps:
            self.assertNotEqual(s["status"], "failed", s)
        self.assertEqual(step(res, "Deep scan for repeated and frozen frames")["status"], "ok")

    def test_steps_are_in_order_and_reported(self):
        res = self.run_analysis()
        names = [s["title"] for s in res.steps]
        self.assertEqual(names[0], "Read the OBS log")
        self.assertEqual(names[-1], "Write the report")
        self.assertLess(names.index("Check every video frame's timestamp"), names.index("Build the timeline of problems"))
        html = read(report.save(res, self.ws.path("reports")))
        self.assertIn("How this recording was checked", html)
        data = weblink.decode(weblink.encode(weblink.to_data(res, "test")))
        self.assertEqual(len(data["steps"]), len(res.steps))

    def test_clean_video_says_no_missing_frames(self):
        clean = fx.cached_video("clean.mkv", seconds=10, gaps=False)
        rep = video_probe.probe(clean)
        self.assertTrue(rep.ts_complete, rep.ts_note)
        self.assertEqual(rep.gap_frames_total, 0)


class WindowsEvents(Base):
    """Finding 1: a relevant Windows event during the recording crashed the whole analysis."""

    def event(self, seconds_in=10):
        return [{"ts": fx.T + timedelta(seconds=seconds_in), "provider": "nvlddmkm", "id": 153, "level": 2,
                 "meaning": "NVIDIA driver reset the GPU (TDR) — the GPU stopped responding briefly",
                 "message": "Display driver nvlddmkm stopped responding and has successfully recovered."}]

    def test_driver_reset_during_recording_is_reported_not_a_crash(self):
        with mock.patch.object(analyze.winevents, "query", return_value=self.event()):
            res = self.run_analysis()
        self.assertTrue(any(i.kind == "winevent" for i in res.incidents), "the driver reset should be on the timeline")
        s = step(res, "Check the Windows event log")
        self.assertEqual(s["status"], "found")
        self.assertIn("1 happened during a recording", s["result"])
        self.assertEqual(step(res, "Write the report")["status"], "ok")

    def test_event_outside_recording_is_not_on_the_timeline(self):
        with mock.patch.object(analyze.winevents, "query", return_value=self.event(seconds_in=100)):
            res = self.run_analysis()
        self.assertFalse(any(i.kind == "winevent" for i in res.incidents))
        self.assertEqual(step(res, "Check the Windows event log")["status"], "ok")

    def test_unreadable_event_log_is_marked_failed_not_clean(self):
        with mock.patch.object(analyze.winevents, "query", side_effect=OSError("access denied")):
            res = self.run_analysis()
        s = step(res, "Check the Windows event log")
        self.assertEqual(s["status"], "failed")
        self.assertNotIn("No relevant Windows events", s["result"])
        self.assertIn("access denied", s["result"])


class DeepScanFailures(Base):
    """Finding 2: the deep scan said "No repeated or frozen frames" even when ffmpeg failed."""

    def assert_no_false_all_clear(self, res):
        s = step(res, "Deep scan for repeated and frozen frames")
        self.assertEqual(s["status"], "failed", s)
        self.assertNotIn("No repeated or frozen frames", s["result"])
        self.assertFalse(res.recordings[0].video.deep_scanned)
        self.assertIn("The deep scan didn't finish", titles(res))
        html = read(report.save(res, self.ws.path("reports")))
        self.assertNotIn("No repeated or frozen frames", html)

    def test_ffmpeg_exits_with_error(self):
        fake = fx.failing_tool(self.ws.dir)
        with mock.patch.object(video_probe, "ffmpeg_path", return_value=fake):
            res = self.run_analysis(deep=True)
        self.assert_no_false_all_clear(res)
        self.assertIn("exit code 1", res.recordings[0].video.deep_note)

    def test_ffmpeg_missing(self):
        with mock.patch.object(video_probe, "ffmpeg_path", return_value=None):
            res = self.run_analysis(deep=True)
        self.assert_no_false_all_clear(res)
        self.assertIn("wasn't found", res.recordings[0].video.deep_note)

    def test_cancelled_scan(self):
        rep = video_probe.probe(self.video)
        video_probe.deep_scan(rep, cancel=lambda: True)
        # a 30 s clip may finish before the first cancel check; either outcome must be honest
        if not rep.deep_scanned:
            self.assertEqual(rep.deep_note, "Cancelled.")


class CutOffFile(Base):
    """A recording cut off partway (drive unplugged, OBS crashed, copy interrupted) must not look complete."""

    def test_truncated_file_is_reported_as_partly_checked(self):
        cut = fx.truncate(self.video, self.ws.path("cut.mkv"), keep=0.5)
        res = self.run_analysis(video=cut, deep=True)
        v = res.recordings[0].video
        self.assertFalse(v.ts_complete, "a half-length read must be flagged")
        self.assertLess(v.ts_checked_s, 25)
        self.assertEqual(step(res, "Check every video frame's timestamp")["status"], "failed")
        self.assertIn("Only part of the file could be checked for missing frames", titles(res))
        self.assertNotIn("No missing frames found in the file", titles(res))
        self.assertFalse(v.deep_scanned, "the deep scan can't vouch for the unread part either")

    def test_ffprobe_error_during_frame_read(self):
        fake = fx.failing_tool(self.ws.dir, code=3)
        real_popen = video_probe.subprocess.Popen

        def popen(cmd, *a, **k):          # file properties read fine, then the frame-by-frame read fails
            if any("packet=pts_time,dts_time" in str(c) for c in cmd):
                cmd = [fake]
            return real_popen(cmd, *a, **k)
        with mock.patch.object(video_probe.subprocess, "Popen", side_effect=popen):
            rep = video_probe.probe(self.video)
        self.assertFalse(rep.ts_complete)
        self.assertIn("exit code 3", rep.ts_note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
