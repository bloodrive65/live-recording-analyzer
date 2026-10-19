"""Parser for OBS Studio log files.

Turns the raw text log into structured data: system info, video/audio
settings, capture sources, recording/streaming sessions, encoder settings
and timestamped warnings.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Optional

LINE_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\.(\d{3}): ?(.*)$")


@dataclass
class LogLine:
    ts: datetime
    text: str
    lineno: int


@dataclass
class VideoSettings:
    ts: datetime
    base_res: str = ""
    output_res: str = ""
    downscale: str = ""
    fps: str = ""
    fps_value: float = 0.0
    format: str = ""
    yuv_mode: str = ""
    color_space: str = ""
    color_range: str = ""


@dataclass
class CaptureDevice:
    ts: datetime
    name: str
    device: str = ""
    resolution: str = ""
    fps: float = 0.0
    format: str = ""
    buffering: str = ""
    hw_decode: str = ""
    sample_rate: int = 0
    uses_device_audio: Optional[bool] = None   # None = unknown
    separate_audio: bool = False               # audio comes from a separate DirectShow audio device
    color_space: str = ""
    color_range: str = ""


@dataclass
class ScreenSource:
    ts: datetime
    kind: str
    name: str
    target: str = ""
    method: str = ""
    force_sdr: str = ""


@dataclass
class Output:
    kind: str  # "recording" or "streaming" or "replay"
    start: datetime
    stop: Optional[datetime] = None
    path: str = ""
    encoder: dict = field(default_factory=dict)
    audio_encoders: list = field(default_factory=list)
    pauses: list = field(default_factory=list)  # [(pause_ts, unpause_ts)]
    frames_output: Optional[int] = None
    frames_drawn: Optional[int] = None
    encode_skipped: Optional[int] = None
    encode_total: Optional[int] = None
    render_lagged: Optional[int] = None
    render_lagged_pct: Optional[float] = None
    dropped_frames: Optional[int] = None
    dropped_total: Optional[int] = None
    video_settings: Optional[VideoSettings] = None
    devices: list = field(default_factory=list)
    screen_sources: list = field(default_factory=list)
    scene: str = ""

    @property
    def paused_seconds(self) -> float:
        total = 0.0
        for p, u in self.pauses:
            end = u or self.stop
            if end:
                total += (end - p).total_seconds()
        return total

    @property
    def duration_seconds(self) -> float:
        if not self.stop:
            return 0.0
        return (self.stop - self.start).total_seconds() - self.paused_seconds

    def wall_to_video(self, ts: datetime) -> Optional[float]:
        """Convert a clock time to a position inside the recording (seconds)."""
        if ts < self.start or (self.stop and ts > self.stop):
            return None
        t = (ts - self.start).total_seconds()
        for p, u in self.pauses:
            u = u or self.stop
            if p <= ts:
                if u and ts >= u:
                    t -= (u - p).total_seconds()
                else:
                    return None  # inside a pause
        return t

    def video_to_wall(self, t: float) -> datetime:
        wall = self.start + timedelta(seconds=t)
        for p, u in self.pauses:
            u = u or self.stop
            if p <= wall and u:
                wall += (u - p)
        return wall


@dataclass
class LogEvent:
    ts: datetime
    category: str
    severity: str  # info / warning / error
    text: str


@dataclass
class ObsLog:
    path: str
    obs_version: str = ""
    system: dict = field(default_factory=dict)
    adapters: list = field(default_factory=list)
    displays: list = field(default_factory=list)
    audio_rate: int = 0
    audio_buffering_sources: list = field(default_factory=list)
    video_settings: list = field(default_factory=list)
    devices: list = field(default_factory=list)
    screen_sources: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    events: list = field(default_factory=list)
    scene_switches: list = field(default_factory=list)
    unclean_shutdown: bool = False
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    browser_errors: int = 0
    plugins: list = field(default_factory=list)
    scene_blocks: list = field(default_factory=list)   # [(ts, {scene: [(source, kind)]})] from "Loaded scenes:"

    def scenes_at(self, ts=None) -> dict:
        """{scene: [(source, kind), ...]} for the scene collection loaded at time ts (latest if None)."""
        best = {}
        for t, sc in self.scene_blocks:
            if ts is None or t <= ts:
                best = sc
        return best

    def scenes_with(self, source: str, ts=None) -> list:
        return [sc for sc, items in self.scenes_at(ts).items() if any(n == source for n, _k in items)]

    def sources_of_kind(self, kinds, ts=None) -> list:
        """[(scene, source, kind)] for sources whose kind is in kinds."""
        return [(sc, n, k) for sc, items in self.scenes_at(ts).items() for n, k in items if k in kinds]

    @property
    def recordings(self):
        return [o for o in self.outputs if o.kind == "recording"]

    def active_at(self, items, ts):
        best = None
        for it in items:
            if it.ts <= ts:
                best = it
        return best

    def to_dict(self):
        return asdict(self)


def range_ui(v: str) -> str:
    """OBS writes “Partial” in its log and config files; every OBS menu calls the same setting “Limited”."""
    x = (v or "").strip().lower()
    if x in ("partial", "limited", "tv", "mpeg"):
        return "Limited"
    if x in ("full", "pc", "jpeg"):
        return "Full"
    return v or ""


def _base_date(path: str, lines: list[str]) -> datetime:
    for raw in lines[:80]:
        m = re.search(r"Current Date/Time: (\d{4}-\d{2}-\d{2}), (\d{2}:\d{2}:\d{2})", raw)
        if m:
            return datetime.strptime(m.group(1), "%Y-%m-%d")
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})[ _-](\d{2})-(\d{2})-(\d{2})", os.path.basename(path))
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"log file uploaded at (\d{4}-\d{2}-\d{2})", "\n".join(lines[:3]))
    if m:
        return datetime.strptime(m.group(1), "%Y-%m-%d")
    return datetime(2000, 1, 1)


def read_lines(path: str) -> list[LogLine]:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read().splitlines()
    base = _base_date(path, raw)
    out: list[LogLine] = []
    day = 0
    prev_secs = None
    for i, r in enumerate(raw, 1):
        m = LINE_RE.match(r.rstrip("\r"))
        if not m:
            continue
        h, mi, s, ms, text = m.groups()
        secs = int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000
        if prev_secs is not None and secs + 1 < prev_secs:
            day += 1  # clock wrapped past midnight
        prev_secs = secs
        ts = base + timedelta(days=day, seconds=secs)
        out.append(LogLine(ts, text.rstrip("\r"), i))
    return out


def _kv(text: str):
    t = text.strip()
    if ":" in t:
        k, v = t.split(":", 1)
        return k.strip().lower(), v.strip()
    return t.lower(), ""


def _fps_value(s: str) -> float:
    s = s.strip()
    m = re.match(r"(\d+)\s*/\s*(\d+)", s)
    if m and int(m.group(2)):
        return int(m.group(1)) / int(m.group(2))
    try:
        return float(s.split()[0])
    except Exception:
        return 0.0


WARN_PATTERNS = [
    (re.compile(r"adding (\d+) milliseconds of audio buffering.*source: (.*)\)", re.I), "audio", "warning"),
    (re.compile(r"Max audio buffering reached", re.I), "audio", "error"),
    (re.compile(r"audio is lagging", re.I), "audio", "error"),
    (re.compile(r"TS_SMOOTHING_THRESHOLD", re.I), "audio", "warning"),
    (re.compile(r"timestamp.*(jump|exceed)", re.I), "timing", "warning"),
    (re.compile(r"nvenc.*(error|fail)|NV_ENC_ERR", re.I), "encoder", "error"),
    (re.compile(r"encoder.*(error|failed)|Encode error|encode failed", re.I), "encoder", "error"),
    (re.compile(r"device (removed|reset|hung)|DXGI_ERROR|Device Removed", re.I), "gpu", "error"),
    (re.compile(r"ffmpeg muxer.*(error|fail)|Failed to write|Write error|disk full|No space", re.I), "disk", "error"),
    (re.compile(r"Crash or unclean shutdown", re.I), "obs", "warning"),
    (re.compile(r"Video configuration failed|Video format match failed|ResolutionValid failed|DecodeDeviceId failed|GetDevice failed", re.I), "capture", "warning"),
    (re.compile(r"WASAPI: Device .* (failed to start|Terminated|invalidated)", re.I), "audio-device", "info"),
    (re.compile(r"Failed to create source", re.I), "obs", "info"),
    (re.compile(r"Source .* (not responding|stalled)", re.I), "capture", "warning"),
]


def parse(path: str) -> ObsLog:
    lines = read_lines(path)
    log = ObsLog(path=path)
    if not lines:
        return log
    log.first_ts, log.last_ts = lines[0].ts, lines[-1].ts

    pending_encoder: dict = {}
    pending_audio: list = []
    current_output: Optional[Output] = None
    current_scene = ""
    i = 0
    n = len(lines)

    def block(start: int):
        """Collect indented continuation lines (tab-prefixed) after index start."""
        j = start + 1
        items = []
        while j < n and (lines[j].text.startswith("\t") or lines[j].text.startswith("  ")) and lines[j].ts - lines[start].ts < timedelta(seconds=2):
            items.append(lines[j].text)
            j += 1
        return items, j

    while i < n:
        L = lines[i]
        t = L.text
        tl = t.lower()

        if t.startswith("OBS ") and "(64-bit" in t or re.match(r"^OBS \d+\.\d+", t):
            log.obs_version = log.obs_version or t.strip()
        elif t.startswith("CPU Name:"):
            log.system["cpu"] = t.split(":", 1)[1].strip()
        elif t.startswith("Physical Cores:"):
            log.system["cores"] = t.split(":", 1)[1].strip()
        elif t.startswith("Physical Memory:"):
            log.system["memory"] = t.split(":", 1)[1].strip()
        elif t.startswith("Windows Version:"):
            log.system["windows"] = t.split(":", 1)[1].strip()
        elif t.startswith("Running as administrator:"):
            log.system["admin"] = t.split(":", 1)[1].strip()
        elif t.strip().startswith("Game Mode:"):
            log.system["game_mode"] = t.split(":", 1)[1].strip()
        elif t.strip().startswith("Game DVR:"):
            log.system["game_dvr"] = t.split(":", 1)[1].strip()
        elif "Crash or unclean shutdown detected" in t:
            log.unclean_shutdown = True
        elif re.match(r"^\tAdapter \d+:", t):
            name = t.split(":", 1)[1].strip()
            ad = {"name": name}
            j = i + 1
            disp = None
            while j < n and lines[j].text.startswith("\t  "):
                k, v = _kv(lines[j].text)
                if k == "hags status":
                    ad["hags"] = v
                elif k == "driver version":
                    ad["driver"] = v
                elif k == "dedicated vram":
                    ad["vram"] = v
                elif k.startswith("output "):
                    disp = {"adapter": name}
                    log.displays.append(disp)
                elif disp is not None:
                    if k.startswith("name="):
                        pass
                    for kk in ("name", "size", "refresh", "bits_per_color", "space", "sdr_white_nits"):
                        if lines[j].text.strip().startswith(kk + "="):
                            disp[kk] = lines[j].text.strip().split("=", 1)[1]
                j += 1
            if not any(a["name"] == name and a.get("driver") == ad.get("driver") for a in log.adapters):
                log.adapters.append(ad)
            i = j
            continue
        elif t.startswith("Loading up D3D11 on adapter"):
            log.system["obs_gpu"] = t.replace("Loading up D3D11 on adapter", "").strip()
        elif t.strip().startswith("samples per sec:"):
            try:
                log.audio_rate = int(t.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif t.startswith("video settings reset:"):
            vs = VideoSettings(ts=L.ts)
            items, j = block(i)
            for it in items:
                k, v = _kv(it)
                if k == "base resolution":
                    vs.base_res = v
                elif k == "output resolution":
                    vs.output_res = v
                elif k == "downscale filter":
                    vs.downscale = v
                elif k == "fps":
                    vs.fps = v
                    vs.fps_value = _fps_value(v)
                elif k == "format":
                    vs.format = v
                elif k == "yuv mode":
                    vs.yuv_mode = v
                    if "/" in v:
                        vs.color_space, vs.color_range = [x.strip() for x in v.split("/", 1)]
                        vs.color_range = range_ui(vs.color_range)
                        vs.yuv_mode = f"{vs.color_space}/{vs.color_range}"
            log.video_settings.append(vs)
            i = j
            continue
        elif re.match(r"^\[DShow Device: '(.*)'\] settings updated", t):
            name = re.match(r"^\[DShow Device: '(.*)'\]", t).group(1)
            dev = CaptureDevice(ts=L.ts, name=name)
            j = i + 1
            while j < n and lines[j].text.startswith("\t") and lines[j].ts - L.ts < timedelta(seconds=2):
                k, v = _kv(lines[j].text)
                if k == "video device":
                    dev.device = v
                elif k == "resolution":
                    dev.resolution = v
                elif k == "fps":
                    dev.fps = _fps_value(v)
                elif k == "format":
                    dev.format = v
                elif k == "buffering":
                    dev.buffering = v
                elif k == "hardware decode":
                    dev.hw_decode = v
                elif k == "separate audio filter":
                    dev.separate_audio = True
                elif k == "using video device audio":
                    dev.uses_device_audio = v.strip().lower().startswith("y")
                elif k == "sample rate":
                    try:
                        dev.sample_rate = int(v)
                    except ValueError:
                        pass
                elif k == "color space":
                    dev.color_space = v
                elif k == "color range":
                    dev.color_range = range_ui(v)
                j += 1
            log.devices.append(dev)
            i = j
            continue
        elif re.match(r"^\[(game-capture|window-capture|duplicator-monitor-capture): '(.*)'\] update settings", t):
            m = re.match(r"^\[(game-capture|window-capture|duplicator-monitor-capture): '(.*)'\]", t)
            src = ScreenSource(ts=L.ts, kind=m.group(1), name=m.group(2))
            j = i + 1
            while j < n and lines[j].text.startswith("\t") and lines[j].ts - L.ts < timedelta(seconds=2):
                k, v = _kv(lines[j].text)
                if k in ("executable", "display", "window"):
                    src.target = src.target or v
                elif k in ("method", "method chosen", "capture mode"):
                    src.method = v
                elif k == "force sdr":
                    src.force_sdr = v
                j += 1
            log.screen_sources.append(src)
            i = j
            continue
        elif re.match(r"^\[(obs-nvenc|jim-nvenc|NVENC encoder|obs-x264|x264 encoder|Texture AMF|AMF|qsv|obs-qsv11|ffmpeg_.*|FFmpeg VAAPI|.*encoder.*)(: '([^']*)')?\] settings:?", t, re.I) and "aac" not in tl and "opus" not in tl:
            m = re.match(r"^\[([^:\]]+)(?:: '([^']*)')?\]", t)
            enc = {"encoder": m.group(1), "name": m.group(2) or "", "ts": L.ts.isoformat()}
            j = i + 1
            while j < n and (lines[j].text.startswith("\t") or lines[j].text.strip() == "") and lines[j].ts - L.ts < timedelta(seconds=2):
                k, v = _kv(lines[j].text)
                if k:
                    enc[k] = v
                j += 1
            # x264 logs everything on one line sometimes
            if "x264" in enc["encoder"].lower():
                for mm in re.finditer(r"(\w[\w-]*)[:=]\s*([^\s]+)", t.split("]", 1)[1] if "]" in t else ""):
                    enc.setdefault(mm.group(1).lower(), mm.group(2))
            pending_encoder = enc
            i = j
            continue
        elif re.search(r"\[(FFmpeg|CoreAudio|Opus).*aac.*encoder|\[FFmpeg (aac|opus) encoder", t, re.I):
            if "not installed" not in tl and "couldn't be loaded" not in tl:
                pending_audio.append(t.split("]", 1)[1].strip() if "]" in t else t)
        elif "==== Recording Start" in t or "==== Streaming Start" in t or "==== Replay Buffer Start" in t:
            kind = "recording" if "Recording" in t else ("streaming" if "Streaming" in t else "replay")
            out = Output(kind=kind, start=L.ts, encoder=pending_encoder, audio_encoders=pending_audio)
            out.video_settings = log.active_at(log.video_settings, L.ts)
            # snapshot latest settings of every capture device / screen source
            latest = {}
            for d in log.devices:
                if d.ts <= L.ts:
                    latest[d.name] = d
            out.devices = list(latest.values())
            latest = {}
            for s in log.screen_sources:
                if s.ts <= L.ts:
                    latest[s.name] = s
            out.screen_sources = list(latest.values())
            out.scene = current_scene
            log.outputs.append(out)
            current_output = out
            pending_encoder, pending_audio = {}, []
        elif re.search(r"Writing file '(.*)'", t) and current_output is not None and not current_output.path:
            current_output.path = re.search(r"Writing file '(.*)'", t).group(1)
        elif re.search(r"output \S+ paused", t) and current_output is not None:
            current_output.pauses.append([L.ts, None])
        elif re.search(r"output \S+ unpaused", t) and current_output is not None:
            if current_output.pauses and current_output.pauses[-1][1] is None:
                current_output.pauses[-1][1] = L.ts
        elif re.search(r"Total frames output: (\d+)", t):
            o = _last_output(log, t)
            if o:
                o.frames_output = int(re.search(r"Total frames output: (\d+)", t).group(1))
        elif re.search(r"Total drawn frames: (\d+)", t):
            o = _last_output(log, t)
            if o:
                o.frames_drawn = int(re.search(r"Total drawn frames: (\d+)", t).group(1))
        elif re.search(r"lagged frames due to rendering lag/stalls: (\d+) \(([\d.]+)%\)", t):
            m = re.search(r"lagged frames due to rendering lag/stalls: (\d+) \(([\d.]+)%\)", t)
            o = _last_output(log, t)
            if o:
                o.render_lagged = int(m.group(1))
                o.render_lagged_pct = float(m.group(2))
        elif re.search(r"skipped frames due to encoding lag: (\d+)/(\d+)", t):
            m = re.search(r"skipped frames due to encoding lag: (\d+)/(\d+)", t)
            o = current_output or (log.outputs[-1] if log.outputs else None)
            if o:
                o.encode_skipped = int(m.group(1))
                o.encode_total = int(m.group(2))
        elif re.search(r"Number of dropped frames due to insufficient bandwidth/connection stalls: (\d+) \(([\d.]+)%\)", t):
            m = re.search(r"stalls: (\d+) \(([\d.]+)%\)", t)
            o = _last_output(log, t)
            if o:
                o.dropped_frames = int(m.group(1))
        elif "==== Recording Stop" in t or "==== Streaming Stop" in t or "==== Replay Buffer Stop" in t:
            kind = "recording" if "Recording" in t else ("streaming" if "Streaming" in t else "replay")
            for o in reversed(log.outputs):
                if o.kind == kind and o.stop is None:
                    o.stop = L.ts
                    if o.pauses and o.pauses[-1][1] is None:
                        o.pauses[-1][1] = L.ts
                    break
            current_output = None
        elif t.strip() == "Loaded scenes:":
            scenes, cur, j = {}, None, i + 1
            while j < n and lines[j].ts - L.ts < timedelta(seconds=5):
                tx = lines[j].text
                m1 = re.match(r"^- scene '(.*)':\s*$", tx)
                m2 = re.match(r"^\s{2,}- source: '(.*)' \(([^)]*)\)\s*$", tx)
                if m1:
                    cur = m1.group(1)
                    scenes[cur] = []
                elif m2 and cur is not None and len(tx) - len(tx.lstrip()) <= 4:
                    scenes[cur].append((m2.group(1), m2.group(2)))
                elif re.match(r"^\s{4,}- ", tx):
                    pass                      # filters and nested group items
                else:
                    break
                j += 1
            log.scene_blocks.append((L.ts, scenes))
            i = j
            continue
        elif re.match(r"^(User switched to scene|Switched to scene) '(.*)'", t):
            current_scene = re.match(r"^(?:User switched to scene|Switched to scene) '(.*)'", t).group(1)
            log.scene_switches.append((L.ts, current_scene))
        elif t.startswith("[obs-browser:") and "Error" in t:
            log.browser_errors += 1

        mab = re.search(r"audio buffering.*\(source: (.*)\)", t)
        if mab:
            log.audio_buffering_sources.append(mab.group(1).strip())   # one entry per event
        for rx, cat, sev in WARN_PATTERNS:
            if rx.search(t):
                log.events.append(LogEvent(L.ts, cat, sev, t.strip()))
                break
        i += 1

    # outputs still open at the end of the log
    for o in log.outputs:
        if o.stop is None:
            o.stop = None
    return log


def _last_output(log: ObsLog, text: str) -> Optional[Output]:
    m = re.search(r"Output '([^']+)'", text)
    name = m.group(1) if m else ""
    kind = None
    if "file_output" in name or "record" in name:
        kind = "recording"
    elif "stream" in name or "rtmp" in name or "whip" in name:
        kind = "streaming"
    elif "replay" in name:
        kind = "replay"
    for o in reversed(log.outputs):
        if kind is None or o.kind == kind:
            return o
    return None


def default_log_dir() -> str:
    appdata = os.environ.get("APPDATA", "")
    return os.path.join(appdata, "obs-studio", "logs") if appdata else ""


def latest_logs(limit: int = 15) -> list[str]:
    d = default_log_dir()
    if not d or not os.path.isdir(d):
        return []
    files = [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(".txt")]
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return files[:limit]
