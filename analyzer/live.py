"""Live advisor: watches OBS, the OBS log, the PC and the recording file in
real time and posts plain-English messages with recommendations.

Phases:
  before  – OBS settings check before you hit Record
  during  – live alerts while recording (skips, drive stalls, overloads…)
  after   – wrap-up when the recording stops, plus automatic analysis
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from . import obs_config, obs_log, video_probe
from .analyze import drive_info, explain, metrics_at, fmt_t

# no console window + below-normal priority, so helper programs never compete with the game or OBS
CREATE_NO_WINDOW = (0x08000000 | 0x00004000) if sys.platform == "win32" else 0


@dataclass
class Advice:
    key: str
    severity: str                 # critical / warning / tip / good / status / request
    phase: str                    # before / during / after
    title: str
    body: str = ""
    steps: list = field(default_factory=list)
    actions: list = field(default_factory=list)   # [(label, action_id, data)]
    ts: datetime = field(default_factory=datetime.now)
    video_t: Optional[float] = None
    count: int = 1
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)
    update: bool = False          # True = replaces an existing card with the same key


ENC_NAMES = {"obs_nvenc_hevc_tex": "NVIDIA NVENC HEVC", "obs_nvenc_h264_tex": "NVIDIA NVENC H.264", "obs_nvenc_av1_tex": "NVIDIA NVENC AV1",
             "jim_hevc_nvenc": "NVIDIA NVENC HEVC", "jim_nvenc": "NVIDIA NVENC H.264", "jim_av1_nvenc": "NVIDIA NVENC AV1",
             "obs_x264": "x264 (CPU)", "h265_texture_amf": "AMD HEVC", "h264_texture_amf": "AMD H.264", "av1_texture_amf": "AMD AV1",
             "obs_qsv11_hevc": "Intel QSV HEVC", "obs_qsv11": "Intel QSV H.264", "obs_qsv11_av1": "Intel QSV AV1",
             "nvenc_hevc": "NVIDIA NVENC HEVC", "nvenc": "NVIDIA NVENC H.264", "nvenc_av1": "NVIDIA NVENC AV1", "x264": "x264 (CPU)"}


def encoder_label(e: str) -> str:
    e = e or ""
    for k, v in ENC_NAMES.items():
        if e.startswith(k) and (len(e) == len(k) or not e[len(k)].isalnum() and e[len(k)] != "_"):
            return v + e[len(k):]
    return e.replace("_", " ")


ENC_HEAVY_PRESETS = ("p5", "p6", "p7", "slow", "slower", "veryslow", "quality", "max quality")


class LiveAdvisor:
    def __init__(self, monitor=None):
        self.monitor = monitor
        self.q: "queue.Queue[Advice]" = queue.Queue()
        self.stop_evt = threading.Event()
        self.thread = None
        self.cfg: Optional[obs_config.ObsConfig] = None
        self.log_path = ""
        self.log_pos = 0
        self.log_obj: Optional[obs_log.ObsLog] = None
        self.hist = deque(maxlen=30)
        self.cooldown = {}
        self.cards = {}         # key -> Advice (for aggregation)
        self.recording = False
        self.rec_start: Optional[datetime] = None
        self.rec_path = ""
        self.rec_skips = {"render": 0, "encode": 0}
        self.paused = False
        self.file_checked = False
        self.want_analysis = None   # (log_path, rec_path) after a recording stops
        self.drive = {}
        self.sustain = {}
        self.last_bytes = None
        self.bytes_stall_since = None
        self.target_fps = 0
        self._now = time.time()
        self._stamp = None
        self._stamp_t = 0
        self._snap = {}
        self.changes = []          # [(time, {setting: (old, new)})] this session

    # ------------------------------------------------------------------ plumbing
    def post(self, a: Advice, cooldown: float = 0):
        now = time.time()
        if cooldown and a.key in self.cooldown and now - self.cooldown[a.key] < cooldown:
            # aggregate into the existing card instead of spamming
            old = self.cards.get(a.key)
            if old:
                old.count += a.count
                old.ts = a.ts
                old.body = a.body
                old.update = True
                self.q.put(old)
            return
        self.cooldown[a.key] = now
        self.cards[a.key] = a
        self.q.put(a)

    def drain(self):
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                return out

    def start(self):
        self.stop_evt.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_evt.set()

    # ------------------------------------------------------------------ OBS settings check
    def preflight(self, announce=True):
        cfg = obs_config.load()
        self.cfg = cfg
        logs = obs_log.latest_logs(1)
        log = None
        if logs:
            try:
                log = obs_log.parse(logs[0])
                self.log_obj = log
            except Exception:
                log = None
        items = preflight_rules(cfg, log)
        if announce:
            for a in reversed(items):   # the app shows newest first; keep the summary on top
                self.post(a)
        self._snap = cfg.snapshot() if cfg.found else {}
        self._stamp = obs_config.settings_stamp()
        if cfg.rec_path:
            self.drive = drive_info(cfg.rec_path)
        self.target_fps = cfg.fps or (log.video_settings[-1].fps_value if log and log.video_settings else 0)
        return items

    # ------------------------------------------------------------------ main loop
    def _loop(self):
        try:
            self.preflight()
        except Exception as e:
            self.post(Advice("pf-error", "warning", "before", "The OBS settings check could not finish", str(e)))
        self._open_log(seek_end=True)
        last_log_check = 0
        while not self.stop_evt.is_set():
            try:
                s = self.monitor.latest if self.monitor else None
                if s and s.get("ts") != getattr(self, "_last_ts", None):
                    self._last_ts = s.get("ts")
                    s = dict(s)
                    s["_dt"] = datetime.fromisoformat(s["ts"])
                    self.hist.append(s)
                    self._on_sample(s)
                if time.time() - last_log_check > 2:
                    last_log_check = time.time()
                    self._tail_log()
                every = 20 if getattr(self.monitor, "light", False) else 8
                if time.time() - self._stamp_t > every:
                    self._stamp_t = time.time()
                    self._check_settings_changed()
            except Exception as e:  # never die
                self.post(Advice("live-error", "warning", "during", "Live advisor hit an error (still running)", repr(e)), cooldown=300)
            self.stop_evt.wait(1.0)

    # ------------------------------------------------------------------ OBS settings changed?
    def _check_settings_changed(self):
        stamp = obs_config.settings_stamp()
        if self._stamp is None or stamp == self._stamp:
            self._stamp = stamp
            return
        self._stamp = stamp
        cfg = obs_config.load()
        if not cfg.found:
            return
        new = cfg.snapshot()
        diff = {k: (self._snap.get(k, "—"), v) for k, v in new.items() if self._snap.get(k) != v}
        diff.update({k: (v, "—") for k, v in self._snap.items() if k not in new})
        self._snap = new
        if not diff:
            return
        self.changes.append((datetime.now(), diff))
        if not self.recording:
            self.cfg = cfg
            self.target_fps = cfg.fps or self.target_fps
            if cfg.rec_path:
                self.drive = drive_info(cfg.rec_path)
        lines = [f"{k}: {a} → {b}" for k, (a, b) in diff.items()]
        when = ("A recording is running, so it keeps using the old settings. They take effect when you start the next recording."
                if self.recording else "These apply to your next recording.")
        effect = change_effects(diff)
        self.post(Advice(f"cfg-change-{time.time():.0f}", "status", "during" if self.recording else "before",
                         "OBS settings changed", when + (" " + effect if effect else "") +
                         " After that recording, the report compares it with the previous one so you can see whether the change helped.",
                         steps=lines))
        if not self.recording:
            # re-check only the new settings, quietly: post anything that is now a real problem
            for a in preflight_rules(cfg, self.log_obj):
                if a.severity in ("critical", "warning") and a.key not in ("pf-summary",):
                    self.post(a, cooldown=600)

    # ------------------------------------------------------------------ log tail
    def _open_log(self, seek_end=False):
        logs = obs_log.latest_logs(1)
        if not logs:
            if not self.log_path:
                self.post(Advice("need-log", "request", "before", "OBS log not found",
                                 "The app looks in %APPDATA%\\obs-studio\\logs. If OBS is installed in portable mode, choose the logs folder manually on the Analyze tab.",
                                 actions=[("Browse for log…", "browse_log", None)]), cooldown=3600)
            return
        if logs[0] != self.log_path:
            self.log_path = logs[0]
            try:
                self.log_pos = os.path.getsize(self.log_path) if seek_end else 0
            except OSError:
                self.log_pos = 0

    def _tail_log(self):
        self._open_log(seek_end=False)
        if not self.log_path:
            return
        try:
            size = os.path.getsize(self.log_path)
            if size < self.log_pos:
                self.log_pos = 0
            if size == self.log_pos:
                return
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self.log_pos)
                chunk = f.read()
                self.log_pos = f.tell()
        except OSError:
            return
        for line in chunk.splitlines():
            m = obs_log.LINE_RE.match(line.rstrip("\r"))
            if not m:
                continue
            self._on_log_line(m.group(5).strip())

    def _on_log_line(self, t: str):
        tl = t.lower()
        m = re.search(r"Writing file '(.*)'", t)
        if m:
            self.rec_path = m.group(1)
            self.drive = drive_info(self.rec_path)
            return
        m = re.search(r"adding (\d+) milliseconds of audio buffering, total audio buffering is now (\d+) milliseconds \(source: (.*)\)", t)
        if m:
            add, total, src = int(m.group(1)), int(m.group(2)), m.group(3)
            self._abuf = getattr(self, "_abuf", {})
            self._abuf[src] = self._abuf.get(src, 0) + 1
            sev = "critical" if total >= 500 else "warning"
            self.post(Advice(f"audiobuf-{src}", sev, "during", f"Audio from “{src}” arrived late — OBS added {add} ms of audio buffering",
                             f"Total audio buffering is now {total} ms. Audio and video may be briefly out of sync around this moment.",
                             steps=audio_late_steps(src, self.log_obj, self._abuf[src]),
                             video_t=self._vt()), cooldown=60)
        elif "max audio buffering reached" in tl or "audio is lagging" in tl:
            self.post(Advice("audiobuf-max", "critical", "during", "OBS hit its maximum audio buffering",
                             "A source keeps delivering audio late. Expect audio dropouts or desync.",
                             steps=["Pause recording and check which audio device is misbehaving (see earlier messages).",
                                    "Restart OBS before the next recording — that resets audio buffering."],
                             actions=[("Pause recording", "pause", None)], video_t=self._vt()), cooldown=120)
        elif re.search(r"Video configuration failed|Video format match failed|ResolutionValid failed|DecodeDeviceId failed", t):
            if self.recording:
                self.post(Advice("capfail", "critical", "during", "Capture device configuration failed",
                                 f"OBS log: {t[:160]}",
                                 steps=["Check the capture device’s cables and that the console, camera or PC feeding it is turned on and sending a picture.",
                                        "Open the device’s properties and press Deactivate → Activate.",
                                        "Make sure the device resolution/FPS is one the card supports for the current input."],
                                 video_t=self._vt()), cooldown=60)
        elif re.search(r"nvenc.*(error|fail)|NV_ENC_ERR|encoder.*error|encode failed", t, re.I):
            self.post(Advice("encerr", "critical", "during", "Encoder error", f"OBS log: {t[:200]}",
                             steps=["Stop and restart the recording.", "Update the GPU driver if this repeats."], video_t=self._vt()), cooldown=60)
        elif re.search(r"(device removed|DXGI_ERROR|device hung)", t, re.I):
            self.post(Advice("gpulost", "critical", "during", "GPU driver reset", f"OBS log: {t[:200]}",
                             steps=["Save your work; OBS may need a restart.", "Check GPU temperatures and update the driver."], video_t=self._vt()), cooldown=60)
        elif re.search(r"skipped frames due to encoding lag: (\d+)/(\d+)", t):
            m = re.search(r"skipped frames due to encoding lag: (\d+)/(\d+)", t)
            sk, tot = int(m.group(1)), int(m.group(2))
            pct = 100 * sk / tot if tot else 0
            self.post(Advice("final-enc", "good" if sk == 0 else ("critical" if pct >= 1 else "warning"), "after",
                             f"OBS final count: {sk:,} of {tot:,} frames skipped due to encoding lag ({pct:.2f}%)"))
        elif re.search(r"lagged frames due to rendering lag/stalls: (\d+) \(([\d.]+)%\)", t):
            m = re.search(r"lagged frames due to rendering lag/stalls: (\d+) \(([\d.]+)%\)", t)
            n = int(m.group(1))
            self.post(Advice("final-ren", "good" if n == 0 else "warning", "after", f"OBS final count: {n:,} frames lagged due to rendering ({m.group(2)}%)"))
        elif re.search(r"failed to write|disk full|no space", tl):
            self.post(Advice("diskfail", "critical", "during", "OBS could not write to the recording file", f"OBS log: {t[:200]}",
                             steps=["Check free space and that the drive is still connected."]), cooldown=60)

    def _vt(self):
        if self.recording and self.monitor and self.monitor.latest and (self.monitor.latest.get("obs") or {}).get("rec_ms"):
            return self.monitor.latest["obs"]["rec_ms"] / 1000
        if self.rec_start:
            return (datetime.now() - self.rec_start).total_seconds()
        return None

    # ------------------------------------------------------------------ samples
    def _sustained(self, key, cond, secs):
        now = self._now
        if cond:
            self.sustain.setdefault(key, now)
            return now - self.sustain[key] >= secs
        self.sustain.pop(key, None)
        return False

    def _on_sample(self, s):
        self._now = s["_dt"].timestamp()
        o = s.get("obs")
        letter = (self.drive or {}).get("letter", "")
        if o is None and self.recording and self.monitor and self.monitor.obs_status != "connected":
            self.post(Advice("obs-lost", "warning", "during", "Lost connection to OBS",
                             "Live skip tracking is paused until OBS reconnects. Monitoring of the PC continues."), cooldown=300)
        if o:
            rec = bool(o.get("rec"))
            if rec and not self.recording:
                self._on_record_start(s)
            elif not rec and self.recording:
                self._on_record_stop(s)
            if rec and o.get("rec_paused") != self.paused:
                self.paused = bool(o.get("rec_paused"))
                self.post(Advice("paused", "status", "during", "Recording paused" if self.paused else "Recording resumed",
                                 video_t=self._vt()))
        if not self.recording:
            return
        win = list(self.hist)[-4:]
        m = metrics_at(win, letter)

        # ---- live skips
        if o:
            for kind, dkey in (("render", "d_render"), ("encode", "d_enc")):
                n = o.get(dkey) or 0
                if n:
                    self.rec_skips[kind] += n
                    causes = explain(kind, m, self.drive)
                    top = causes[0][0] if causes else ""
                    ckey = _cause_key(top)
                    title = (f"{n} frame(s) missed — rendering lag" if kind == "render" else f"{n} frame(s) skipped — encoding lag")
                    body = ("Likely: " + top) if top else ""
                    steps = recommend(kind, ckey, self.cfg, m, self.drive)
                    acts = [("Pause recording", "pause", None)] if ckey in ("encoder", "disk") else []
                    total = self.rec_skips[kind]
                    a = Advice(f"skip-{kind}-{ckey}", "critical" if total >= 60 else "warning", "during", title,
                               body + f"  (Total this recording: {total:,})", steps=steps, actions=acts, video_t=self._vt(), count=n)
                    self.post(a, cooldown=45)

        # ---- early warnings (before skips happen)
        enc = max([v for v in (m.get("gpu_enc"), m.get("nv_enc")) if isinstance(v, (int, float))] or [0])
        if self._sustained("enc", enc >= 95, 8):
            if self.rec_skips["encode"]:
                self.post(Advice("warn-enc", "warning", "during", f"Video encoder is at its limit ({enc:.0f}%) and frames are being skipped",
                                 f"{self.rec_skips['encode']:,} frame(s) skipped so far in this recording.",
                                 steps=recommend("encode", "encoder", self.cfg, m, self.drive), actions=[("Pause recording", "pause", None)],
                                 video_t=self._vt()), cooldown=300)
            else:
                self.post(Advice("note-enc", "tip", "during", f"Video encoder is busy ({enc:.0f}%), no skipped frames so far",
                                 "A busy encoder on its own is fine — what matters is skipped frames. If any happen, you’ll get an alert "
                                 "with what to change. Nothing to do right now.",
                                 steps=["If skips do start, these are the settings to lighten:"] + recommend("encode", "encoder", self.cfg, m, self.drive),
                                 video_t=self._vt()), cooldown=900)
        g3 = m.get("gpu_3d") or 0
        if self._sustained("g3", g3 >= 97, 8):
            self.post(Advice("warn-g3", "warning", "during", f"GPU is maxed out ({g3:.0f}% 3D load)",
                             "OBS needs some GPU time every frame. When the GPU is full, rendering lag follows.",
                             steps=recommend("render", "gpu", self.cfg, m, self.drive), video_t=self._vt()), cooldown=300)
        wms = m.get("disk_write_ms") or 0
        if self._sustained("disk", wms >= 300, 2):
            self.post(Advice("warn-disk", "warning", "during", f"Recording drive {letter} is stalling ({wms:.0f} ms per write)",
                             "OBS can absorb short stalls, but long ones cause skipped frames.",
                             steps=recommend("encode", "disk", self.cfg, m, self.drive), video_t=self._vt()), cooldown=180)
        if (m.get("ram") or 0) >= 92 and self._sustained("ram", True, 10):
            self.post(Advice("warn-ram", "warning", "during", f"Memory almost full ({m['ram']:.0f}%)",
                             "Windows may start paging to disk, which causes stutters.",
                             steps=["Close browsers and other large programs you don’t need while recording."], video_t=self._vt()), cooldown=600)
        elif (m.get("ram") or 0) < 92:
            self.sustain.pop("ram", None)
        if (m.get("nv_temp") or 0) >= 85 and self._sustained("temp", True, 15):
            self.post(Advice("warn-temp", "warning", "during", f"GPU is hot ({m['nv_temp']:.0f} °C)",
                             "Hot GPUs slow down, which can cause lag.", steps=["Improve airflow / fan curve; clean dust filters."], video_t=self._vt()), cooldown=900)
        if o and self.target_fps and not o.get("rec_paused"):
            low = o.get("fps", 0) < self.target_fps * 0.95
            if self._sustained("fps", low, 5):
                self.post(Advice("warn-fps", "warning", "during", f"OBS is running below target ({o.get('fps', 0):.1f} of {self.target_fps:g} fps)",
                                 "OBS can’t keep its own frame rate — usually the GPU is overloaded.",
                                 steps=recommend("render", "gpu", self.cfg, m, self.drive), video_t=self._vt()), cooldown=300)
        # file stopped growing?
        if o and not o.get("rec_paused"):
            b = o.get("rec_bytes")
            if b is not None:
                if self.last_bytes is not None and b <= self.last_bytes:
                    self.bytes_stall_since = self.bytes_stall_since or self._now
                    if self._now - self.bytes_stall_since >= 6:
                        self.post(Advice("file-stall", "critical", "during", "The recording file has stopped growing",
                                         f"No data written for {self._now - self.bytes_stall_since:.0f} s. The drive may have disconnected or stalled.",
                                         steps=["Check that the recording drive is still connected.",
                                                "If it doesn’t recover, stop the recording and start a new one on another drive."],
                                         video_t=self._vt()), cooldown=120)
                else:
                    self.bytes_stall_since = None
                self.last_bytes = b
            # free space
            mbs = o.get("rec_mbs")
            free_mb = o.get("disk_free_mb")
            if mbs and mbs > 0.5 and free_mb:
                mins = free_mb / mbs / 60
                if mins < 20:
                    self.post(Advice("space", "critical" if mins < 5 else "warning", "during",
                                     f"About {mins:.0f} minutes of recording space left",
                                     f"Writing ~{mbs * 8:.0f} Mb/s with {free_mb / 1024:.1f} GB free.",
                                     steps=["Stop soon, or free up space / change the recording path for the next recording."],
                                     video_t=self._vt()), cooldown=300)
            if o.get("stream") and (o.get("stream_congestion") or 0) > 0.5:
                self.post(Advice("stream-cong", "warning", "during", "Stream connection is congested",
                                 "Your upload can’t keep up — viewers will see dropped frames.",
                                 steps=["Lower the stream bitrate, or use a wired connection."]), cooldown=300)
        # quick check of the new file ~25 s in
        if not self.file_checked and self.rec_start and (s["_dt"] - self.rec_start).total_seconds() > 25:
            self.file_checked = True
            threading.Thread(target=self._quick_file_check, daemon=True).start()

    def _on_record_start(self, s):
        self.recording = True
        self.paused = False
        self.rec_start = s["_dt"]
        self.rec_skips = {"render": 0, "encode": 0}
        self.file_checked = False
        self.last_bytes = None
        self.sustain.clear()
        try:
            self.cfg = obs_config.load()
            if self.cfg.rec_path:
                self.drive = drive_info(self.cfg.rec_path)
            self.target_fps = self.cfg.fps or self.target_fps
        except Exception:
            pass
        d = self.drive or {}
        where = f" to {d.get('letter')}" + (f" ({d.get('name')})" if d.get("name") else "") if d.get("letter") else ""
        self.post(Advice(f"rec-start-{self.rec_start:%H%M%S}", "status", "during", f"Recording started{where}",
                         "Watching for skipped frames, drive stalls and overloads. The file itself gets checked in about 25 seconds."))

    def _on_record_stop(self, s):
        self.recording = False
        dur = (s["_dt"] - self.rec_start).total_seconds() if self.rec_start else 0
        r, e = self.rec_skips["render"], self.rec_skips["encode"]
        sev = "good" if (r + e) == 0 else ("critical" if (r + e) >= 60 else "warning")
        self.post(Advice(f"rec-stop-{s['_dt']:%H%M%S}", sev, "after",
                         f"Recording finished — {fmt_t(dur)}",
                         f"Live count: {e:,} encoding-lag skips, {r:,} rendering-lag frames." +
                         (" Clean recording." if (r + e) == 0 else " The full report will show when and why."),
                         actions=[("Analyze this recording now", "analyze_last", None)]))
        tips = after_tips(self.cfg, r + e)
        for a in tips:
            self.post(a)
        self.want_analysis = (self.log_path, self.rec_path)
        self.rec_start = None

    def _quick_file_check(self):
        path = self.rec_path
        if not path:
            # fall back to newest file in the recording folder
            folder = (self.cfg.rec_path if self.cfg else "") or ""
            try:
                files = [os.path.join(folder, f) for f in os.listdir(folder)]
                files = [f for f in files if os.path.isfile(f) and f.lower().endswith((".mkv", ".mp4", ".mov", ".flv", ".ts"))]
                path = max(files, key=os.path.getmtime) if files else ""
            except Exception:
                path = ""
        if not path or not os.path.isfile(path):
            self.post(Advice("fcheck-none", "request", "during", "Couldn’t find the file being recorded",
                             "It will be analyzed after you stop. If you record somewhere unusual, you can point the app to it then.",
                             actions=[("Locate file…", "locate_video", None)]))
            return
        a = quick_file_check(path, self.cfg)
        if a:
            for x in a:
                self.post(x)


# ---------------------------------------------------------------------------- rule helpers
def _cause_key(text: str) -> str:
    t = text.lower()
    if "encoder" in t:
        return "encoder"
    if "drive" in t:
        return "disk"
    if "gpu was maxed" in t or "3d load" in t:
        return "gpu"
    if "cpu" in t:
        return "cpu"
    if "programs" in t or "disk activity" in t:
        return "other"
    return "unknown"


def recommend(kind, ckey, cfg, m, drive) -> list:
    es = (cfg.encoder_settings if cfg else {}) or {}
    preset = str(es.get("preset2") or es.get("preset") or "").lower()
    steps = []
    path = cfg.enc_path if cfg else (lambda o: f"OBS → Settings → Output → {o}")
    if ckey == "encoder":
        if "x264" in str(cfg.rec_encoder if cfg else "").lower():
            steps.append("You’re encoding on the CPU (x264). " + path("Video Encoder") + " → pick your graphics card’s hardware encoder "
                         "(NVIDIA NVENC, AMD AMF/VCE or Intel QuickSync).")
        elif preset in ENC_HEAVY_PRESETS:
            steps.append(f"Pause recording, then {path('Preset')} → change {preset.upper()} to a faster one (e.g. P4 or P3).")
        else:
            steps.append(f"Pause recording, then {path('Preset')} → choose a faster one.")
        if str(es.get("multipass", "")).lower() in ("fullres", "2"):
            steps.append(path("Multipass Mode") + " → “Two Passes (Quarter Resolution)” or “Single Pass”.")
        if es.get("lookahead"):
            steps.append(path("Look-ahead") + " → untick it.")
        steps.append("If it still can’t keep up, lower the recording resolution or frame rate (OBS → Settings → Video), or try a different codec (HEVC/H.264/AV1).")
    elif ckey == "disk":
        letter = (drive or {}).get("letter") or "the recording drive"
        rp = cfg.rec_path_setting() if cfg else "OBS → Settings → Output → Recording Path"
        steps.append(f"{letter} couldn’t keep up. After this recording, change {rp} to an internal SSD and move files later.")
        if (drive or {}).get("bus", "").upper() == "USB":
            steps.append("USB drives can go to sleep mid-recording. Windows: Control Panel → Power Options → Change plan settings → "
                         "Change advanced power settings → Hard disk → Turn off hard disk after → Never, and "
                         "USB settings → USB selective suspend setting → Disabled.")
        steps.append("Avoid copying files or running backups to the same drive while recording.")
    elif ckey == "gpu":
        steps.append("Cap the game’s frame rate a little below your monitor’s refresh rate: use the game’s own limiter, or "
                     "NVIDIA Control Panel → Manage 3D Settings → Max Frame Rate, or AMD Software → Gaming → Graphics → Frame Rate Target Control.")
        steps.append("Run OBS as administrator so it gets GPU priority.")
        if cfg and cfg.browser_sources >= 3:
            steps.append(f"Your scene collection has {cfg.browser_sources} browser sources — hide/remove ones you don’t need.")
        if cfg and cfg.base_res and cfg.output_res and cfg.base_res != cfg.output_res:
            steps.append("OBS → Settings → Video → Downscale Filter → Bicubic (lighter than Lanczos).")
    elif ckey == "cpu":
        steps.append("Close heavy background programs (see the busiest programs listed in the report).")
    elif ckey == "other":
        steps.append("Another program spiked at that moment — close it or schedule it for after recording.")
    else:
        steps.append("No clear cause yet — keep playing. The after-recording report will check the file and the full timeline.")
    return steps


def change_effects(diff: dict) -> str:
    """What a settings change is expected to do, in plain words."""
    out = []
    order = ["p1", "p2", "p3", "p4", "p5", "p6", "p7"]
    if "Preset" in diff:
        a, b = (str(x).split(":")[0].lower() for x in diff["Preset"])
        if a in order and b in order:
            if order.index(b) < order.index(a):
                out.append("A faster preset gives the video encoder less work per frame, which lowers the chance of encoding skips "
                           "(at a slightly lower quality for the same file size).")
            elif order.index(b) > order.index(a):
                out.append("A slower preset gives the encoder more work per frame — better quality, but more risk of encoding skips.")
    if "Multipass mode" in diff:
        out.append("Multipass changes how much work the encoder does per frame (Single pass is the lightest).")
    if "Look-ahead" in diff:
        out.append("Look-ahead adds encoder work; turning it off makes encoding lighter.")
    if "Output resolution" in diff or "FPS" in diff:
        out.append("Resolution and frame rate have the biggest effect on encoder and GPU load.")
    return " ".join(out)


def audio_rate_fix(dev, obs_rate: int) -> list:
    """Correct steps for a capture device whose audio runs at a different rate than OBS."""
    if dev.separate_audio or dev.uses_device_audio is False:
        return [f"The source takes its sound from a separate audio device at {dev.sample_rate / 1000:g} kHz (OBS runs at {obs_rate / 1000:g} kHz).",
                "Option A: double-click the source → untick “Use custom audio device” so OBS takes the sound straight from the video device.",
                "Option B: add the sound as its own source instead: Sources → + → Audio Input Capture → pick the capture device’s audio, "
                "then mute the video source in the Audio Mixer so you don’t hear it twice.",
                f"Windows: Control Panel → Sound → Recording tab → that audio device → Properties → Advanced → Default Format → {obs_rate} Hz (if offered)."]
    return [f"Windows: Control Panel → Sound → Recording tab → the capture device → Properties → Advanced → Default Format → {obs_rate} Hz (if offered),",
            f"or change OBS → Settings → Audio → General → Sample Rate to {dev.sample_rate} Hz to match the device."]


def audio_late_steps(src: str, log, times: int = 2) -> list:
    """Only blame things that actually apply to this source."""
    steps = []
    dev = None
    if log:
        for d in log.devices:
            if d.name == src:
                dev = d
    if times >= 2 and dev is not None and dev.sample_rate and log.audio_rate and dev.sample_rate != log.audio_rate:
        steps += audio_rate_fix(dev, log.audio_rate)
    if dev is not None:
        steps.append("Plug the capture device straight into a USB port on the PC (rear ports are best), not a hub or front-panel port.")
    steps.append("One message when a source starts is normal. If it keeps happening: right-click the source → Deactivate, then Activate, "
                 "or restart OBS between recordings.")
    return steps


def preflight_rules(cfg: obs_config.ObsConfig, log: Optional[obs_log.ObsLog]) -> list:
    A = []
    P = "before"
    if not cfg.found and not log:
        A.append(Advice("pf-noobs", "request", P, "Couldn’t find OBS settings",
                        "OBS stores them in %APPDATA%\\obs-studio. Is OBS installed (non-portable)? You can still analyze logs and videos on the Analyze tab.",
                        actions=[("Browse for log…", "browse_log", None)]))
        return A
    ws = cfg.websocket
    if ws and not ws.get("server_enabled", False):
        A.append(Advice("pf-ws", "request", P, "Turn on OBS WebSocket (one-time)",
                        "Lets this app see the exact second OBS skips a frame and pause recording for you.",
                        steps=["In OBS: Tools → WebSocket Server Settings.", "Tick “Enable WebSocket server” → click OK.",
                               "Come back here — the connection status turns green within a few seconds and the password is read automatically."]))
    # color
    if cfg.found:
        es = cfg.encoder_settings or {}
        prof = str(es.get("profile", "")).lower()
        if cfg.color_range.lower() == "full":
            A.append(Advice("pf-range", "warning", P, "Change color range to Limited",
                            "Full range is often shown with crushed blacks or washed-out colors in players, editors and on YouTube.",
                            steps=["OBS → Settings → Advanced → Video → Color Range → Limited.", "Click Apply before you start recording."]))
        tenbit_fmt = cfg.color_format.upper() in ("P010", "I010", "P216", "P416")
        hdr_space = cfg.color_space.startswith("2100")
        if not tenbit_fmt and prof in ("main10", "high10"):
            A.append(Advice("pf-10bit", "tip", P, "Encoder is set to a 10-bit profile, but OBS makes 8-bit pictures",
                            f"Color Format is {cfg.color_format} (8-bit) while the encoder profile is {es.get('profile')}.",
                            steps=["For normal SDR recording: " + cfg.enc_path("Profile") + " → main.",
                                   "For real 10-bit/HDR: Settings → Advanced → Color Format → P010 and Color Space → Rec. 2100 (PQ), with an HDR source."]))
        if hdr_space and not tenbit_fmt:
            A.append(Advice("pf-hdr8", "critical", P, "HDR color space with an 8-bit color format",
                            "Rec. 2100 needs P010. Colors will be wrong.",
                            steps=["Settings → Advanced → Color Format → P010, or set Color Space back to Rec. 709."]))
        if tenbit_fmt and not hdr_space and prof not in ("main10", "high10", ""):
            A.append(Advice("pf-p010", "tip", P, "P010 color format with an 8-bit encoder profile",
                            steps=["Use profile main10 (HEVC) / 10-bit AV1, or switch Color Format back to NV12."]))
        if hdr_space and cfg.capture_devices:
            A.append(Advice("pf-hdrcap", "tip", P, "HDR recording with a capture card",
                            "The device feeding the capture card must send HDR, and the card must pass it through, or the picture will look dull.",
                            steps=["Turn HDR on in the console or source device’s video settings (for example PS5: Settings → Screen and Video → Video Output → HDR).",
                                   "Check that your capture card supports HDR capture and that it’s enabled in its own software, if it has a setting for it."]))
        # fps / devices
        for d in cfg.capture_devices:
            if d.get("fps") and cfg.fps and abs(d["fps"] - cfg.fps) > 0.01:
                ratio = d["fps"] / cfg.fps
                if abs(ratio - round(ratio)) < 0.002 and round(ratio) >= 2:
                    A.append(Advice(f"pf-fps2-{d['name']}", "tip", P, f"“{d['name']}” runs at {d['fps']:g} fps, OBS at {cfg.fps:g}",
                                    "OBS simply drops the extra frames. This works fine; matching them only saves a little USB bandwidth.",
                                    steps=[f"Optional: double-click “{d['name']}” → Resolution/FPS Type: Custom → FPS: {cfg.fps:g} (or “Match Output FPS”)."]))
                else:
                    period = 1 / abs(d["fps"] - cfg.fps)
                    A.append(Advice(f"pf-fps-{d['name']}", "warning", P, f"Frame-rate mismatch: “{d['name']}” {d['fps']:g} fps vs OBS {cfg.fps:g} fps",
                                    f"Causes a repeated or dropped frame about every {period:.0f} seconds (micro-stutter).",
                                    steps=[f"Double-click “{d['name']}” → Resolution/FPS Type: Custom → FPS: {cfg.fps:g} (or “Match Output FPS”).",
                                           f"Or change OBS → Settings → Video → Common FPS Values to {d['fps']:g}."]))
            dr = str(d.get("color_range", "default")).lower()
            if dr in ("full", "partial") and ((dr == "full") != (cfg.color_range.lower() == "full")):
                A.append(Advice(f"pf-devrange-{d['name']}", "tip", P, f"“{d['name']}” color range is forced to {'Full' if dr == 'full' else 'Limited'}",
                                f"OBS output is {cfg.range_label}. If this doesn’t match what the source device sends, blacks look grey or crushed.",
                                steps=[f"Double-click “{d['name']}” → Color Range → Default, unless you have a reason to force it.",
                                       "Or match the source device’s own RGB range setting (for example PS5: Settings → Screen and Video → Video Output → RGB Range)."]))
        # screen captures vs HDR desktop
        hdr_desktop = log and any("G2084" in x.get("space", "") for x in log.displays)
        if hdr_desktop and not hdr_space:
            bad = [s["name"] for s in cfg.screen_captures if not s.get("force_sdr")]
            if bad:
                A.append(Advice("pf-forcesdr", "tip", P, "Windows HDR is on — PC captures may look washed out",
                                f"These captures don’t have “Force SDR” ticked: {', '.join(bad[:5])}{'…' if len(bad) > 5 else ''}. "
                                "Ignore this if you only record your capture card.",
                                steps=["Display Capture / Window Capture: double-click it → tick “Force SDR”.",
                                       "Game Capture has no Force SDR box. If an HDR game looks wrong in the recording, double-click it and check Color Space "
                                       "(sRGB for a normal SDR recording).",
                                       "Or turn Windows HDR off while recording PC games: Windows Settings → System → Display → Use HDR."]))
        # format / encoder
        fmt = (cfg.rec_format or "").lower()
        if fmt in ("mp4", "mov"):
            A.append(Advice("pf-mp4", "tip", P, f"Recording format is {fmt.upper()}",
                            "Only matters if something crashes: a plain MP4/MOV that isn’t finished properly can’t be opened.",
                            steps=[cfg.rec_format_setting() + " → Hybrid MP4 (if listed) or Matroska Video (.mkv)."]))
        enc_name = (cfg.rec_encoder or "").lower()
        if "x264" in enc_name and cfg.output_res and int(cfg.output_res.split("x")[0] or 0) >= 2560:
            A.append(Advice("pf-x264", "warning", P, "CPU encoding (x264) at high resolution",
                            "Very likely to cause encoding lag at this resolution.",
                            steps=[cfg.enc_path("Video Encoder") + " → your graphics card’s hardware encoder (NVIDIA NVENC, AMD AMF/VCE or Intel QuickSync)."]))
        preset = str(es.get("preset2") or es.get("preset") or "").lower()
        big = cfg.output_res and int((cfg.output_res.split("x")[0] or 0)) >= 2560 and cfg.fps >= 59
        heavy = []
        if preset in ENC_HEAVY_PRESETS:
            heavy.append(f"Preset {preset.upper()}")
        if str(es.get("multipass", "")).lower() == "fullres":
            heavy.append("Multipass full resolution")
        if es.get("lookahead"):
            heavy.append("Look-ahead")
        last_rec = log.recordings[-1] if (log and log.recordings) else None
        had_skips = bool(last_rec and last_rec.encode_skipped)
        if heavy and big:
            A.append(Advice("pf-heavy", "warning" if had_skips else "tip", P, "Encoder settings are heavy for " + f"{cfg.output_res} @ {cfg.fps:g}",
                            ("Your last recording had encoding skips, and these settings are a likely reason: " if had_skips else
                             "No skips so far, so this is only a heads-up. If skips show up, these are the first things to lighten: ") + ", ".join(heavy) + ".",
                            steps=[cfg.enc_path("Preset") + " → P4 or faster; Multipass Mode → Quarter Resolution or Single Pass; Look-ahead → off."]))
        if cfg.base_res and cfg.output_res and cfg.base_res != cfg.output_res:
            A.append(Advice("pf-scale", "tip", P, f"OBS scales {cfg.base_res} → {cfg.output_res}",
                            "Scaling costs GPU time every frame.", steps=["Settings → Video → Downscale Filter → Bicubic if you see rendering lag."]))
        if cfg.rec_rescale:
            A.append(Advice("pf-rescale", "tip", P, f"Recording rescale is on ({cfg.rec_rescale_res})", "An extra scaling pass per frame.",
                            steps=[cfg.enc_path("Rescale Output") + " → untick it unless you need it."]))
        if cfg.browser_sources >= 6:
            A.append(Advice("pf-browser", "tip", P, f"{cfg.browser_sources} browser sources in this scene collection",
                            "Each one is a small web browser using CPU/GPU.", steps=["Tick “Shutdown source when not visible” on widgets, remove unused ones."]))
        # drive
        if cfg.rec_path:
            d = drive_info(cfg.rec_path)
            free = None
            try:
                import shutil as _sh
                free = _sh.disk_usage(cfg.rec_path).free / 1073741824
            except Exception:
                pass
            desc = " ".join(x for x in (d.get("name"), f"({d.get('bus')} {d.get('media')})" if d.get("bus") else "") if x)
            if d.get("type") == "network":
                A.append(Advice("pf-net", "warning", P, f"Recording to a network drive ({cfg.rec_path})",
                                "Network shares pause unpredictably.", steps=["Record to a local SSD and move files afterwards."]))
            elif d.get("bus", "").upper() == "USB":
                A.append(Advice("pf-usb", "tip", P, f"Recording to a USB drive {d.get('letter')} {desc}",
                                "Usually OK, but some USB drives stall. While watching is on, the app tracks its write speed and warns you if it falls behind."))
            if free is not None and free < 50:
                A.append(Advice("pf-space", "warning" if free < 20 else "tip", P, f"Only {free:.0f} GB free on the recording drive",
                                "4K60 HEVC can use 40–80 GB per hour.", steps=["Free up space or pick another drive before long sessions."]))
            elif free is not None:
                A.append(Advice("pf-space-ok", "good", P, f"Recording drive {d.get('letter') or ''} has {free:,.0f} GB free"))
            if not os.path.isdir(cfg.rec_path) and sys.platform == "win32":
                A.append(Advice("pf-path", "critical", P, "Recording folder doesn’t exist", f"{cfg.rec_path} was not found — is the drive connected?",
                                steps=["Connect the drive, or change " + cfg.rec_path_setting() + "."]))
    # from the log
    if log:
        if log.system.get("admin", "").lower() == "false":
            A.append(Advice("pf-admin", "tip", P, "Run OBS as administrator",
                            "Lets OBS raise its GPU priority so games can’t starve it.",
                            steps=["Close OBS → right-click its shortcut → Properties → Compatibility → Run this program as an administrator."]))
        if log.last_ts and log.first_ts and (log.last_ts - log.first_ts) > timedelta(hours=12):
            A.append(Advice("pf-uptime", "tip", P, f"OBS has been open for {(log.last_ts - log.first_ts).total_seconds() / 3600:.0f} hours",
                            "Restarting OBS before a long session clears built-up memory and audio buffering.", steps=["Close and reopen OBS."]))
        last = log.recordings[-1] if log.recordings else None
        if last and last.encode_skipped:
            pct = 100 * last.encode_skipped / max(last.encode_total or 1, 1)
            A.append(Advice("pf-history", "status", P, f"Last recording had {last.encode_skipped:,} encoding skips ({pct:.2f}%)",
                            "While watching is on, the app will show what caused them."))
    if cfg.found:
        es = cfg.encoder_settings or {}
        summary = (f"{cfg.output_res or cfg.base_res} @ {cfg.fps_label} fps · {cfg.color_format} · {cfg.space_label} · {cfg.range_label} range · "
                   f"{encoder_label(cfg.rec_encoder)} {str(es.get('preset2') or es.get('preset') or '').upper()} "
                   f"{str(es.get('rate_control') or '')} {('CQ ' + str(es.get('cqp'))) if es.get('cqp') else ''} · {(cfg.rec_format or '').upper()} → {cfg.rec_path}")
        bad = [a for a in A if a.severity in ("critical", "warning")]
        A.insert(0, Advice("pf-summary", "good" if not bad else "status", P,
                           "OBS settings check: " + ("settings look good" if not bad else f"{len(bad)} thing(s) to fix before you record"),
                           f"Profile “{cfg.profile}”: {summary}"))
    return A


def after_tips(cfg, skips) -> list:
    out = []
    fmt = (cfg.rec_format if cfg else "") or ""
    if fmt.lower() == "mkv":
        out.append(Advice("after-remux", "tip", "after", "Editing? Remux the MKV first",
                          "Most editors prefer MP4. Remuxing is lossless and takes seconds.",
                          steps=["OBS → File → Remux Recordings → pick the file → Remux.",
                                 "Or have OBS do it for you: Settings → Advanced → Recording → tick “Automatically remux to MP4”."]))
    if skips:
        out.append(Advice("after-deep", "tip", "after", "Run a deep scan to see the skips in the video",
                          "The deep scan finds repeated/frozen frames and their exact positions, so you can check them in your editor.",
                          actions=[("Run deep scan", "deep_last", None)]))
    return out


def quick_file_check(path: str, cfg) -> list:
    """Check the first seconds of a file that is still being recorded."""
    fp = video_probe.ffprobe_path()
    if not fp:
        return []
    out = []
    try:
        r = subprocess.run([fp, "-v", "error", "-show_streams", "-of", "json", path], capture_output=True, text=True,
                           timeout=30, creationflags=CREATE_NO_WINDOW)
        info = json.loads(r.stdout or "{}")
    except Exception:
        return [Advice("fcheck-fail", "tip", "during", "Couldn’t read the file yet — it will be checked after you stop.")]
    vs = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    aus = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not vs:
        return [Advice("fcheck-novideo", "critical", "during", "The recording has no video stream yet",
                       "OBS may be recording only audio, or the file is unreadable.", steps=["Check your scene/sources and the recording encoder."])]
    v = vs[0]
    issues = []
    fps = video_probe._ratio(v.get("r_frame_rate") or "0/1")
    if fps > 240:
        fps = video_probe._ratio(v.get("avg_frame_rate") or "0/1")
    res = f"{v.get('width')}x{v.get('height')}"
    rng = {"pc": "Full", "tv": "Limited"}.get(v.get("color_range", ""), "untagged")
    space = v.get("color_space", "untagged")
    bits = "10-bit" if "10" in (v.get("pix_fmt") or "") else "8-bit"
    if cfg and cfg.found:
        want_res = cfg.rec_rescale_res if cfg.rec_rescale and cfg.rec_rescale_res else cfg.output_res
        if want_res and res != want_res:
            issues.append(f"resolution is {res}, settings say {want_res}")
        if cfg.fps and fps and abs(fps - cfg.fps) > 0.01:
            issues.append(f"frame rate is {fps:.3f}, settings say {cfg.fps:g}")
        if rng != "untagged" and rng != cfg.range_label:
            issues.append(f"color range tag is {rng}, settings say {cfg.range_label}")
        want_space = {"709": "bt709", "601": "smpte170m", "2100PQ": "bt2020nc", "2100HLG": "bt2020nc"}.get(cfg.color_space)
        if want_space and space not in ("untagged", want_space, "bt470bg" if want_space == "smpte170m" else want_space):
            issues.append(f"color space tag is {space}, settings say {cfg.space_label}")
        for a in aus[:1]:
            sr = int(a.get("sample_rate") or 0)
            if sr and cfg.sample_rate and sr != cfg.sample_rate:
                issues.append(f"audio is {sr} Hz, OBS is {cfg.sample_rate} Hz")
    # gaps in the first 20 seconds
    gaps = 0
    try:
        r = subprocess.run([fp, "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+20", "-show_entries", "packet=pts_time",
                            "-of", "csv=p=0", path], capture_output=True, text=True, timeout=60, creationflags=CREATE_NO_WINDOW)
        pts = sorted(float(x) for x in r.stdout.split() if x.replace(".", "", 1).isdigit())
        if fps and len(pts) > 10:
            fr = 1 / fps
            gaps = sum(int(round((b - a) / fr)) - 1 for a, b in zip(pts, pts[1:]) if b - a > fr * 1.5)
    except Exception:
        pass
    desc = f"{res} · {fps:.3f}".rstrip("0").rstrip(".") + f" fps · {v.get('codec_name', '').upper()} {v.get('profile', '')} · {bits} · {rng} range · {space}"
    if aus:
        desc += f" · audio {aus[0].get('codec_name')} {aus[0].get('sample_rate')} Hz"
    if issues:
        out.append(Advice("fcheck", "warning", "during", "File check: the recording doesn’t match your settings",
                          desc + ". Problems: " + "; ".join(issues) + ".",
                          steps=["If this is wrong, stop now, fix the setting, and start a new recording — it’s cheaper than fixing it later."]))
    else:
        out.append(Advice("fcheck", "good", "during", "File check passed", desc))
    if gaps:
        out.append(Advice("fcheck-gaps", "warning", "during", f"{gaps} frame(s) already missing in the first 20 seconds",
                          "Something is dropping frames from the start.", steps=["Watch the next messages for the cause."]))
    return out
