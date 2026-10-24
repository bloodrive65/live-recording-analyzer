"""Throwaway test data: a made-up OBS log, matching watching data, and small generated videos.

Everything is written into a fresh temporary folder, so tests never read or change a real OBS log,
real reports or real settings. Nothing here comes from a real PC.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from analyzer import video_probe  # noqa: E402

T = datetime(2026, 10, 18, 14, 0, 0)          # recording start in the fixture log
LOG_NAME = "2026-10-18 13-55-00.txt"

HEADER = """Current Date/Time: 2026-10-18, 13:55:00
CPU Name: Example 8-Core Processor
CPU Speed: 4000MHz
Physical Cores: 8, Logical Cores: 16
Physical Memory: 32000MB Total, 20000MB Free
Windows Version: 10.0 Build 26100 (release: 24H2; revision: 1; 64-bit)
Running as administrator: false
OBS 32.0.0 (64-bit, windows)
---------------------------------
audio settings reset:
\tsamples per sec: 48000
\tspeakers:        2
---------------------------------
Available Video Adapters: 
\tAdapter 0: NVIDIA GeForce RTX 4060 Ti
\t  Driver Version: 32.0.16.1088
Loading up D3D11 on adapter NVIDIA GeForce RTX 4060 Ti (0)
---------------------------------
video settings reset:
\tbase resolution:   640x360
\toutput resolution: 640x360
\tdownscale filter:  Lanczos
\tfps:               60/1
\tformat:            NV12
\tYUV mode:          Rec. 709/Partial
---------------------------------"""


def tool(name: str) -> str:
    p = video_probe.ffmpeg_path() if name == "ffmpeg" else video_probe.ffprobe_path()
    if not p:
        raise RuntimeError(f"{name} is needed for the tests (put it on PATH)")
    return p


def _ts(d: datetime) -> str:
    return d.strftime("%H:%M:%S.") + f"{d.microsecond // 1000:03d}"


def write_log(path: str, video_name: str = "test.mkv", seconds: int = 30) -> str:
    t0 = T - timedelta(minutes=5)
    lines = [f"{_ts(t0)}: {l}" for l in HEADER.splitlines()]
    L = lambda d, t: lines.append(f"{_ts(d)}: {t}")
    L(T - timedelta(seconds=60), "[DShow Device: 'Video Capture Device'] settings updated: ")
    for t in ["\tvideo device: Elgato 4K X", "\tresolution: 3840x2160", "\tfps: 59.94 (interval: 166833)", "\tformat: NV12"]:
        L(T - timedelta(seconds=60), t)
    L(T - timedelta(seconds=1), "[obs-nvenc: 'advanced_video_recording'] settings:")
    for t in ["\tcodec:        HEVC", "\trate_control: CQP", "\tcqp:          21", "\tpreset:       p5",
              "\twidth:        640", "\theight:       360"]:
        L(T - timedelta(seconds=1), t)
    L(T, "==== Recording Start ===============================================")
    L(T, f"[ffmpeg muxer: 'adv_file_output'] Writing file 'Z:/Recordings/{video_name}'...")
    L(T + timedelta(seconds=8), "adding 64 milliseconds of audio buffering, total audio buffering is now 277 milliseconds "
                                "(source: Video Capture Device)")
    end = T + timedelta(seconds=seconds)
    L(end, f"[ffmpeg muxer: 'adv_file_output'] Output of file 'Z:/Recordings/{video_name}' stopped")
    L(end, f"Output 'adv_file_output': Total frames output: {seconds * 60 - 8}")
    L(end, f"Output 'adv_file_output': Total drawn frames: {seconds * 60}")
    L(end, f"Video stopped, number of skipped frames due to encoding lag: 8/{seconds * 60} (0.4%)")
    L(end, "==== Recording Stop ================================================")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def write_monitor(path: str) -> str:
    with open(path, "w") as f:
        f.write(json.dumps({"type": "session_start", "ts": (T - timedelta(seconds=10)).isoformat()}) + "\n")
        es = 0
        for i in range(-10, 40):
            d = T + timedelta(seconds=i)
            s = {"type": "sample", "ts": d.isoformat(timespec="milliseconds"), "cpu": 22, "cpu_core_max": 60, "ram": 48,
                 "gpu_3d": 45, "gpu_enc": 70, "disks": {"Z:": {"busy": 12, "write_ms": 9, "write_mbs": 20, "queue": 0.1}},
                 "procs": {"cpu": [["obs64.exe", 9]], "io": [["obs64.exe", 20, 0]]}}
            o = {"fps": 60, "render_ms": 3.1, "render_skip": 0, "enc_skip": es, "rec": 0 <= i <= 30}
            if i == 5:
                s["disks"]["Z:"] = {"busy": 100, "write_ms": 1450, "write_mbs": 0.3, "queue": 9}
                es += 6
                o["enc_skip"], o["d_enc"] = es, 6
            s["obs"] = o
            f.write(json.dumps(s) + "\n")
    return path


def make_video(path: str, seconds: int = 30, gaps: bool = True) -> str:
    """640x360 60 fps H.264 + AAC. With gaps=True, frames 300-305 and 900-901 are removed but the
    timestamps of the other frames are kept, which is exactly what missing frames look like in a real file."""
    vf = "select='not(between(n\\,300\\,305))*not(between(n\\,900\\,901))'" if gaps else "null"
    subprocess.run([tool("ffmpeg"), "-y", "-v", "error",
                    "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size=640x360:rate=60",
                    "-f", "lavfi", "-i", f"sine=duration={seconds}",
                    "-vf", vf, "-fps_mode", "passthrough", "-c:v", "libx264", "-preset", "ultrafast", "-g", "60",
                    "-c:a", "aac", path], check=True, capture_output=True)
    return path


def truncate(src: str, dst: str, keep: float = 0.5) -> str:
    """Copy only the first part of a file, like a recording on a drive that disconnected mid-copy."""
    with open(src, "rb") as f:
        data = f.read()
    with open(dst, "wb") as f:
        f.write(data[: int(len(data) * keep)])
    return dst


def failing_tool(folder: str, code: int = 1) -> str:
    """A stand-in for ffmpeg that starts fine, prints nothing useful and exits with an error."""
    if sys.platform == "win32":
        p = os.path.join(folder, "fake_ffmpeg.cmd")
        with open(p, "w") as f:
            f.write(f"@echo [error] Invalid data found when processing input 1>&2\r\n@exit /b {code}\r\n")
    else:
        p = os.path.join(folder, "fake_ffmpeg")
        with open(p, "w") as f:
            f.write(f"#!/bin/sh\necho '[error] Invalid data found when processing input' 1>&2\nexit {code}\n")
        os.chmod(p, 0o755)
    return p


class Workspace:
    """A temporary folder that looks like a PC with OBS: APPDATA/obs-studio/logs/<log>, plus test files."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="lra-test-")
        self.appdata = os.path.join(self.dir, "appdata")
        self.logs = os.path.join(self.appdata, "obs-studio", "logs")
        os.makedirs(self.logs)
        self.log = write_log(os.path.join(self.logs, LOG_NAME))
        self.monitor = write_monitor(os.path.join(self.dir, "monitor.jsonl"))

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


_VIDEO_CACHE: dict = {}


def cached_video(name: str, **kw) -> str:
    """Generate each test video once per run."""
    key = (name, tuple(sorted(kw.items())))
    if key not in _VIDEO_CACHE or not os.path.isfile(_VIDEO_CACHE[key]):
        d = tempfile.mkdtemp(prefix="lra-vid-")
        _VIDEO_CACHE[key] = make_video(os.path.join(d, name), **kw)
    return _VIDEO_CACHE[key]
