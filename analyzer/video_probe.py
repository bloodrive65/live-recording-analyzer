"""Recording file analysis using ffprobe / ffmpeg.

* Stream info (resolution, frame rate, color tags, codec, profile)
* Frame timestamp gaps (missing frames) and frame-rate drift
* Audio gaps and audio/video length drift
* Optional deep scan for repeated (duplicate) and frozen frames
"""
from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _tool(name: str) -> Optional[str]:
    exe = name + (".exe" if sys.platform == "win32" else "")
    here = getattr(sys, "_MEIPASS", None)
    candidates = []
    if here:
        candidates += [os.path.join(here, "bin", exe), os.path.join(here, exe)]
    base = os.path.dirname(os.path.abspath(sys.argv[0]))
    candidates += [os.path.join(base, "bin", exe), os.path.join(base, exe)]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return shutil.which(name)


def ffprobe_path():
    return _tool("ffprobe")


def ffmpeg_path():
    return _tool("ffmpeg")


@dataclass
class Gap:
    t: float            # position in the video (seconds)
    missing: int        # estimated missing frames
    length_ms: float    # time span of the gap


@dataclass
class DupRun:
    t: float
    frames: int


@dataclass
class VideoReport:
    path: str
    ok: bool = True
    error: str = ""
    container: str = ""
    duration: float = 0.0
    size_bytes: int = 0
    bitrate: int = 0
    video: dict = field(default_factory=dict)
    audio: list = field(default_factory=list)
    frame_count: int = 0
    expected_frames: int = 0
    nominal_fps: float = 0.0
    measured_fps: float = 0.0
    vfr: bool = False
    gaps: list = field(default_factory=list)
    gap_frames_total: int = 0
    backwards_ts: int = 0
    audio_gaps: list = field(default_factory=list)
    av_drift_ms: float = 0.0
    av_start_offset_ms: float = 0.0
    fps_timeline: list = field(default_factory=list)  # [(t, fps)] per 10 s window
    deep_scanned: bool = False
    dup_isolated: list = field(default_factory=list)   # times of single/double repeats
    dup_runs: list = field(default_factory=list)       # longer freezes
    dup_total: int = 0
    dup_periodic_s: Optional[float] = None
    tiny_packets: int = 0

    def to_dict(self):
        return asdict(self)


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                          creationflags=CREATE_NO_WINDOW, **kw)


def _ratio(s: str) -> float:
    try:
        a, b = s.split("/")
        return float(a) / float(b) if float(b) else 0.0
    except Exception:
        try:
            return float(s)
        except Exception:
            return 0.0


def probe(path: str, progress: Optional[Callable[[str, float], None]] = None,
          deep: bool = False, cancel: Optional[Callable[[], bool]] = None) -> VideoReport:
    rep = VideoReport(path=path)
    fp = ffprobe_path()
    if not fp:
        rep.ok, rep.error = False, "ffprobe was not found."
        return rep
    if not os.path.isfile(path):
        rep.ok, rep.error = False, "Recording file not found (it may have been moved or deleted)."
        return rep
    rep.size_bytes = os.path.getsize(path)
    say = progress or (lambda m, p: None)

    say("Reading recording details…", 0.02)
    r = _run([fp, "-v", "error", "-show_format", "-show_streams", "-of", "json", path])
    try:
        info = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        info = {}
    if not info.get("streams"):
        rep.ok, rep.error = False, "ffprobe could not read this file. " + (r.stderr or "")[:300]
        return rep
    fmt = info.get("format", {})
    rep.container = fmt.get("format_long_name") or fmt.get("format_name", "")
    rep.duration = float(fmt.get("duration") or 0)
    rep.bitrate = int(fmt.get("bit_rate") or 0)
    vstreams = [s for s in info["streams"] if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    astreams = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    if not vstreams:
        rep.ok, rep.error = False, "No video stream in file."
        return rep
    v = vstreams[0]
    rep.video = {
        "index": v.get("index"),
        "codec": v.get("codec_name"),
        "profile": v.get("profile"),
        "width": v.get("width"),
        "height": v.get("height"),
        "pix_fmt": v.get("pix_fmt"),
        "color_range": v.get("color_range", "unknown"),
        "color_space": v.get("color_space", "unknown"),
        "color_transfer": v.get("color_transfer", "unknown"),
        "color_primaries": v.get("color_primaries", "unknown"),
        "r_frame_rate": v.get("r_frame_rate"),
        "avg_frame_rate": v.get("avg_frame_rate"),
        "bits_per_raw_sample": v.get("bits_per_raw_sample"),
        "field_order": v.get("field_order", ""),
    }
    for a in astreams:
        rep.audio.append({
            "index": a.get("index"), "codec": a.get("codec_name"),
            "sample_rate": int(a.get("sample_rate") or 0), "channels": a.get("channels"),
            "title": (a.get("tags") or {}).get("title", ""),
        })
    rep.nominal_fps = _ratio(v.get("r_frame_rate") or "0/1") or _ratio(v.get("avg_frame_rate") or "0/1")
    if rep.nominal_fps > 240 or rep.nominal_fps <= 0:  # mkv sometimes reports 1000/1 timebase
        rep.nominal_fps = _ratio(v.get("avg_frame_rate") or "0/1")
    rep.expected_frames = int(round(rep.duration * rep.nominal_fps)) if rep.nominal_fps else 0

    # ---- video packet timestamps -------------------------------------------------
    say("Checking every frame's timestamp…", 0.05)
    cmd = [fp, "-v", "error", "-select_streams", str(v.get("index", 0)), "-show_entries",
           "packet=pts_time,dts_time,size,flags", "-of", "csv=p=0", path]
    pts, sizes, flags = [], [], []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            errors="replace", creationflags=CREATE_NO_WINDOW, bufsize=1 << 16)
    exp = max(rep.expected_frames, 1)
    prev_dts = None
    for line in proc.stdout:
        parts = line.strip().split(",")
        if len(parts) < 3:
            continue
        try:
            p = float(parts[0]) if parts[0] not in ("", "N/A") else None
            d = float(parts[1]) if parts[1] not in ("", "N/A") else None
            sz = int(parts[2]) if parts[2] not in ("", "N/A") else 0
        except ValueError:
            continue
        if d is not None and prev_dts is not None and d < prev_dts:
            rep.backwards_ts += 1
        if d is not None:
            prev_dts = d
        if p is None:
            p = d
        if p is None:
            continue
        pts.append(p)
        sizes.append(sz)
        flags.append(parts[3] if len(parts) > 3 else "")
        if len(pts) % 5000 == 0:
            say(f"Checking frame timestamps… {len(pts):,} frames", 0.05 + 0.40 * min(len(pts) / exp, 1.0))
            if cancel and cancel():
                proc.kill()
                rep.ok, rep.error = False, "Cancelled"
                return rep
    proc.wait()
    pts.sort()
    rep.frame_count = len(pts)
    if len(pts) > 2:
        span = pts[-1] - pts[0]
        rep.measured_fps = (len(pts) - 1) / span if span > 0 else 0
        fps = rep.nominal_fps or rep.measured_fps
        frame = 1.0 / fps if fps else 0
        if frame:
            deltas = [b - a for a, b in zip(pts, pts[1:])]
            # VFR: noticeable spread of frame durations
            off = sum(1 for d in deltas if abs(d - frame) > frame * 0.25)
            rep.vfr = off > max(10, len(deltas) * 0.01)
            for idx, d in enumerate(deltas):
                if d > frame * 1.5:
                    missing = int(round(d / frame)) - 1
                    if missing >= 1:
                        rep.gaps.append(Gap(t=round(pts[idx] - pts[0], 3), missing=missing,
                                            length_ms=round(d * 1000, 1)))
                        rep.gap_frames_total += missing
            # fps per 10-second window
            win = 10.0
            start = pts[0]
            bucket, count = 0, 0
            for p in pts:
                b = int((p - start) // win)
                if b != bucket:
                    rep.fps_timeline.append((round(bucket * win, 1), round(count / win, 2)))
                    bucket, count = b, 0
                count += 1
            # tiny packets often mean the encoder got a repeated (identical) frame
            non_key = [s for s, f in zip(sizes, flags) if "K" not in f and s > 0]
            if len(non_key) > 100:
                med = statistics.median(non_key)
                rep.tiny_packets = sum(1 for s in non_key if s < med * 0.02)

    # ---- audio --------------------------------------------------------------------
    if astreams:
        say("Checking audio continuity…", 0.47)
        a = astreams[0]
        r = _run([fp, "-v", "error", "-select_streams", str(a.get("index")), "-show_entries",
                  "packet=pts_time,duration_time", "-of", "csv=p=0", path])
        apts = []
        for line in r.stdout.splitlines():
            parts = line.strip().split(",")
            try:
                apts.append((float(parts[0]), float(parts[1]) if len(parts) > 1 and parts[1] not in ("", "N/A") else 0.0))
            except (ValueError, IndexError):
                continue
        apts.sort()
        if len(apts) > 2 and pts:
            for (p0, d0), (p1, _) in zip(apts, apts[1:]):
                dur = d0 or 0.021
                if p1 - p0 > dur * 2.5 and p1 - p0 > 0.05:
                    rep.audio_gaps.append({"t": round(p0 - pts[0], 3), "length_ms": round((p1 - p0 - dur) * 1000, 1)})
            a_end = apts[-1][0] + (apts[-1][1] or 0)
            v_end = pts[-1] + (1.0 / rep.nominal_fps if rep.nominal_fps else 0)
            rep.av_drift_ms = round((a_end - v_end) * 1000, 1)
            rep.av_start_offset_ms = round((apts[0][0] - pts[0]) * 1000, 1)

    # ---- deep scan --------------------------------------------------------------------
    if deep:
        deep_scan(rep, progress=say, cancel=cancel)
    say("Recording check finished", 1.0)
    return rep


DROP_RE = re.compile(r"\b(drop|keep)\b.*?pts_time:([\d.]+)")


def deep_scan(rep: VideoReport, progress=None, cancel=None):
    ff = ffmpeg_path()
    if not ff:
        return
    say = progress or (lambda m, p: None)
    say("Deep scan: looking for repeated/frozen frames (this can take a while)…", 0.5)
    vidx = rep.video.get("index", 0)
    cmd = [ff, "-hide_banner", "-nostats", "-hwaccel", "auto", "-i", rep.path, "-map", f"0:{vidx}",
           "-an", "-vf", "scale=320:-2:flags=fast_bilinear,mpdecimate=max=0:hi=64*6:lo=64*2:frac=0.1",
           "-loglevel", "debug", "-f", "null", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                            errors="replace", creationflags=CREATE_NO_WINDOW, bufsize=1 << 16)
    drops = []
    first = None
    last_report = 0.0
    total = rep.duration or 1
    for line in proc.stderr:
        if "mpdecimate" not in line:
            continue
        m = DROP_RE.search(line)
        if not m:
            continue
        t = float(m.group(2))
        if first is None:
            first = t
        if m.group(1) == "drop":
            drops.append(t - first)
        if t - last_report > 15:
            last_report = t
            say(f"Deep scan… {int(t//60)} min of {int(total//60)} min", 0.5 + 0.48 * min(t / total, 1))
            if cancel and cancel():
                proc.kill()
                return
    proc.wait()
    rep.deep_scanned = True
    rep.dup_total = len(drops)
    frame = 1.0 / rep.nominal_fps if rep.nominal_fps else 1 / 60
    # group consecutive repeats
    runs = []
    for t in drops:
        if runs and t - runs[-1][1] <= frame * 1.6:
            runs[-1][1] = t
            runs[-1][2] += 1
        else:
            runs.append([t, t, 1])
    for s, e, c in runs:
        if c <= 2:
            rep.dup_isolated.append(round(s, 3))
        else:
            rep.dup_runs.append(DupRun(t=round(s, 3), frames=c))
    # periodic pattern? (e.g. 59.94 vs 60 → a repeat every ~16.7 s)
    iso = rep.dup_isolated
    if len(iso) >= 4:
        intervals = [b - a for a, b in zip(iso, iso[1:]) if b - a > 0.5]
        if len(intervals) >= 3:
            med = statistics.median(intervals)
            close = sum(1 for x in intervals if abs(x - med) < med * 0.15)
            if close >= len(intervals) * 0.6:
                rep.dup_periodic_s = round(med, 2)
