"""Combine the OBS log, the recording file(s) and monitor data into findings."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from . import obs_log as obslog
from . import video_probe
from . import winevents
from .monitor import load_session

# no console window + below-normal priority, so helper programs never compete with the game or OBS
CREATE_NO_WINDOW = (0x08000000 | 0x00004000) if sys.platform == "win32" else 0
SEV_ORDER = {"critical": 0, "warning": 1, "info": 2, "good": 3}


@dataclass
class Finding:
    severity: str
    category: str
    title: str
    detail: str
    why: str = ""
    fix: str = ""
    recording: str = ""
    loc: list = field(default_factory=list)    # [[label, value]] — exactly where this comes from, on this PC


@dataclass
class Incident:
    start: datetime
    end: datetime
    kind: str                 # "render", "encode", "file-gap", "audio-gap", "freeze", "repeat", "log", "winevent", "spike"
    title: str
    frames: int = 0
    video_t: Optional[float] = None
    metrics: dict = field(default_factory=dict)
    causes: list = field(default_factory=list)  # [(text, confidence)]
    recording: str = ""


@dataclass
class RecordingResult:
    output: obslog.Output
    video: Optional[video_probe.VideoReport] = None
    drive: dict = field(default_factory=dict)
    samples: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    incidents: list = field(default_factory=list)
    label: str = ""


@dataclass
class Result:
    created: datetime
    log: Optional[obslog.ObsLog]
    recordings: list
    findings: list
    incidents: list
    winevents: list
    monitor_path: str = ""
    monitor_meta: dict = field(default_factory=dict)
    samples: list = field(default_factory=list)
    marks: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    steps: list = field(default_factory=list)      # every step the analysis took, in order (see steps.py)


def fmt_t(sec: Optional[float]) -> str:
    if sec is None:
        return "—"
    sec = max(0, sec)
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def _res(s: str):
    m = re.match(r"(\d+)\s*x\s*(\d+)", s or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def drive_info(path: str) -> dict:
    """Describe the drive a recording was written to (bus type, HDD/SSD, network)."""
    info = {"letter": "", "type": "unknown", "bus": "", "media": "", "name": ""}
    m = re.match(r"^([A-Za-z]):", path or "")
    if not m:
        if (path or "").startswith("\\\\") or (path or "").startswith("//"):
            info["type"] = "network"
        return info
    letter = m.group(1).upper()
    info["letter"] = letter + ":"
    if sys.platform != "win32":
        return info
    try:
        import ctypes
        t = ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\")
        info["type"] = {2: "removable", 3: "fixed", 4: "network", 5: "cdrom", 6: "ramdisk"}.get(t, "unknown")
    except Exception:
        pass
    if info["type"] in ("fixed", "removable"):
        ps = (f"$p=Get-Partition -DriveLetter {letter} -ErrorAction SilentlyContinue; if($p){{$d=$p|Get-Disk;"
              f"$pd=Get-PhysicalDisk|?{{$_.DeviceId -eq $d.Number}};"
              f"Write-Output ($d.BusType.ToString()+'|'+$pd.MediaType+'|'+$d.FriendlyName)}}")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                               text=True, timeout=25, creationflags=CREATE_NO_WINDOW)
            parts = r.stdout.strip().split("|")
            if len(parts) == 3:
                info["bus"], info["media"], info["name"] = [p.strip() for p in parts]
        except Exception:
            pass
    return info


def _window(samples, start: datetime, end: datetime):
    return [s for s in samples if start <= s["_dt"] <= end]


def _mx(rows, key, sub=None):
    vals = []
    for r in rows:
        v = r.get(key)
        if sub and isinstance(v, dict):
            v = v.get(sub)
        if isinstance(v, (int, float)):
            vals.append(v)
    return max(vals) if vals else None


def _disk(rows, letter, key):
    vals = []
    for r in rows:
        d = (r.get("disks") or {}).get(letter) or {}
        v = d.get(key)
        if isinstance(v, (int, float)):
            vals.append(v)
    return max(vals) if vals else None


KNOWN_GAME_HINTS = ("obs64.exe", "obs32.exe")


def metrics_at(rows, letter) -> dict:
    m = {
        "gpu_3d": _mx(rows, "gpu_3d"), "gpu_enc": _mx(rows, "gpu_enc"), "cpu": _mx(rows, "cpu"),
        "cpu_core": _mx(rows, "cpu_core_max"), "ram": _mx(rows, "ram"),
        "disk_busy": _disk(rows, letter, "busy") if letter else None,
        "disk_write_ms": _disk(rows, letter, "write_ms") if letter else None,
        "disk_queue": _disk(rows, letter, "queue") if letter else None,
        "disk_mbs": _disk(rows, letter, "write_mbs") if letter else None,
        "nv_enc": None, "nv_temp": None, "nv_clock": None, "obs_render_ms": None,
    }
    m["nv_enc"] = max([r["nv"]["enc"] for r in rows if r.get("nv") and isinstance(r["nv"].get("enc"), (int, float))] or [None]) if any(r.get("nv") and isinstance(r["nv"].get("enc"), (int, float)) for r in rows) else None
    temps = [r["nv"]["temp"] for r in rows if r.get("nv") and isinstance(r["nv"].get("temp"), (int, float))]
    m["nv_temp"] = max(temps) if temps else None
    rms = [r["obs"]["render_ms"] for r in rows if r.get("obs")]
    m["obs_render_ms"] = max(rms) if rms else None
    fps = [r["obs"]["fps"] for r in rows if r.get("obs")]
    m["obs_fps_min"] = min(fps) if fps else None
    # other busy programs
    procs = {}
    io = {}
    for r in rows:
        p = r.get("procs") or {}
        for name, cpu in p.get("cpu", []):
            procs[name] = max(procs.get(name, 0), cpu)
        for name, w, rd in p.get("io", []):
            io[name] = max(io.get(name, 0), w + rd)
    m["top_cpu"] = sorted(procs.items(), key=lambda x: -x[1])[:4]
    m["top_io"] = sorted(io.items(), key=lambda x: -x[1])[:3]
    gp = {}
    for r in rows:
        for name, v in r.get("gpu_procs") or []:
            gp[name] = max(gp.get(name, 0), v)
    m["top_gpu"] = sorted(gp.items(), key=lambda x: -x[1])[:3]
    thr = [r["nv"].get("throttle") for r in rows if r.get("nv") and r["nv"].get("throttle")]
    m["nv_throttle"] = thr[-1] if thr else None
    return m


def explain(kind: str, m: dict, drive: dict, base_cpu: float | None = None) -> list:
    """Return likely reasons for an incident given system metrics around it."""
    c = []
    g3, ge, ne = m.get("gpu_3d"), m.get("gpu_enc"), m.get("nv_enc")
    enc = max([x for x in (ge, ne) if isinstance(x, (int, float))] or [None]) if (ge is not None or ne is not None) else None
    if kind in ("render", "file-gap", "repeat", "encode"):
        if g3 is not None and g3 >= 95:
            c.append((f"GPU was maxed out ({g3:.0f}% 3D load) — OBS could not get GPU time to compose/convert the frame", "high" if kind == "render" else "medium"))
        elif g3 is not None and g3 >= 85:
            c.append((f"GPU 3D load was high ({g3:.0f}%)", "low"))
    if kind in ("encode", "file-gap", "repeat"):
        if enc is not None and enc >= 95:
            c.append((f"Video encoder chip (NVENC/AMF/QSV) was at its limit ({enc:.0f}%) — settings too heavy for this resolution/FPS", "high"))
        elif enc is not None and enc >= 85:
            c.append((f"Video encoder was close to its limit ({enc:.0f}%)", "medium"))
    busy, wms, q = m.get("disk_busy"), m.get("disk_write_ms"), m.get("disk_queue")
    letter = drive.get("letter") or "the recording drive"
    if (wms is not None and wms >= 200) or (busy is not None and busy >= 95) or (q is not None and q >= 4):
        desc = []
        if busy is not None:
            desc.append(f"{busy:.0f}% busy")
        if wms is not None:
            desc.append(f"{wms:.0f} ms per write")
        if q is not None:
            desc.append(f"queue {q:.0f}")
        extra = ""
        if drive.get("bus", "").upper() == "USB" or drive.get("type") == "removable":
            extra = " (USB drive — enclosures can stall or spin down)"
        elif drive.get("type") == "network":
            extra = " (network drive)"
        c.append((f"Recording drive {letter} was struggling ({', '.join(desc)}){extra}", "high" if (wms or 0) >= 500 else "medium"))
    cpu, core = m.get("cpu"), m.get("cpu_core")
    if cpu is not None and cpu >= 90:
        c.append((f"CPU was maxed out ({cpu:.0f}%)", "high"))
    elif core is not None and core >= 99 and (cpu or 0) >= 50:
        c.append((f"At least one CPU core was pinned at 100% (overall {cpu:.0f}%)", "low"))
    if m.get("ram") is not None and m["ram"] >= 92:
        c.append((f"Memory nearly full ({m['ram']:.0f}% RAM used)", "medium"))
    if m.get("nv_temp") is not None and m["nv_temp"] >= 83:
        c.append((f"GPU was hot ({m['nv_temp']:.0f} °C) — it may have slowed itself down", "low"))
    thr = m.get("nv_throttle")
    if thr and thr not in ("0x0000000000000000", "0x0000000000000001", "Not Active", "[N/A]"):
        c.append((f"GPU reported clock throttling (reason code {thr})", "low"))
    others = [(n, v) for n, v in m.get("top_cpu", []) if v >= 20 and n.lower() not in KNOWN_GAME_HINTS]
    if others:
        c.append(("Other programs were using a lot of CPU: " + ", ".join(f"{n} {v:.0f}%" for n, v in others[:3]), "medium"))
    io_others = [(n, v) for n, v in m.get("top_io", []) if v >= 40 and n.lower() not in KNOWN_GAME_HINTS]
    if io_others:
        c.append(("Heavy disk activity from: " + ", ".join(f"{n} {v:.0f} MB/s" for n, v in io_others), "medium"))
    if not c:
        if any(m.get(k) is not None for k in ("gpu_3d", "cpu", "disk_busy")):
            c.append(("No obvious system bottleneck at that moment — could be a brief driver/USB hiccup from the capture card or an OBS internal stall", "low"))
        else:
            c.append(("No system monitor data for this moment — run the monitor during your next session to see what the PC was doing", "low"))
    return c


def _cluster(points, gap_s=3.0):
    """points: list of (datetime, frames). Returns [(start, end, frames)]."""
    out = []
    for t, n in sorted(points):
        if out and (t - out[-1][1]).total_seconds() <= gap_s:
            out[-1][1] = t
            out[-1][2] += n
        else:
            out.append([t, t, n])
    return out


# ---------------------------------------------------------------------------- main
def run(log_path: Optional[str], video_paths: dict | None = None, monitor_path: Optional[str] = None,
        deep: bool = False, progress: Optional[Callable[[str, float], None]] = None,
        cancel: Optional[Callable[[], bool]] = None, recordings_filter: Optional[list] = None) -> Result:
    """video_paths: {recording index -> file path} (paths may be overridden if files moved)."""
    from .steps import StepLog
    say = progress or (lambda m, p: None)
    findings: list[Finding] = []
    incidents: list[Incident] = []
    notes = []
    st = StepLog()
    CHK_LOG = ("OBS version, your encoder and its settings, output resolution and frame rate, color format, color space and range, "
               "scenes and sources, when each recording started, paused and stopped, skipped and lagged frame counts, and OBS warnings")
    CHK_MON = ("This app's once-a-second PC readings while you recorded: GPU and video-encoder load, CPU, RAM, recording-drive activity "
               "and speed, temperatures, the busiest programs, and OBS's own frame counters")

    log = None
    if log_path:
        say("Reading OBS log…", 0.01)
        st.begin("Read the OBS log", CHK_LOG)
        log = obslog.parse(log_path)
        span = f"; the log covers {log.first_ts:%b %d %I:%M %p} to {log.last_ts:%I:%M %p}" if log.first_ts and log.last_ts else ""
        st.done(f"{os.path.basename(log_path)}: OBS {log.obs_version or 'version unknown'}, {len(log.recordings)} recording(s){span}.")
    else:
        st.skip("Read the OBS log", CHK_LOG, "No OBS log was used (video file only), so OBS settings and skip counts weren't checked.")

    samples, marks, meta = [], [], {}
    if monitor_path and os.path.isfile(monitor_path):
        say("Loading monitor data…", 0.03)
        st.begin("Load this app's watching data", CHK_MON)
        meta, samples, marks = load_session(monitor_path)
        st.done(f"{len(samples):,} readings" + (f" from {samples[0]['_dt']:%I:%M:%S %p} to {samples[-1]['_dt']:%I:%M:%S %p}." if samples else "."))
    else:
        st.skip("Load this app's watching data", CHK_MON,
                "This app wasn't watching during this recording, so the report can't say what the PC was doing at each problem.")

    recs = []
    if log:
        all_recs = log.recordings
        for idx, o in enumerate(all_recs):
            if recordings_filter is not None and idx not in recordings_filter:
                continue
            r = RecordingResult(output=o, label=f"Recording {idx + 1} — {o.start:%b %d %I:%M %p}")
            r.drive = drive_info(o.path)
            recs.append((idx, r))
    elif video_paths:
        # No log: analyze files alone
        for idx, p in video_paths.items():
            fake = obslog.Output(kind="recording", start=datetime.fromtimestamp(os.path.getmtime(p)) if os.path.exists(p) else datetime.now(), path=p)
            r = RecordingResult(output=fake, label=os.path.basename(p))
            r.drive = drive_info(p)
            recs.append((idx, r))

    # ---- system-wide findings from the log
    CHK_SYS = "Whether OBS runs as administrator, an unclean shutdown, Windows HDR vs. screen captures, browser source errors, and how long OBS was open"
    n0 = len(findings)
    if log:
        st.begin("Check OBS-wide settings", CHK_SYS)
        sysf = log.system
        if sysf.get("admin", "").lower() == "false":
            findings.append(Finding("warning", "System", "OBS is not running as administrator",
                                    "OBS logged “Running as administrator: false”.",
                                    "Without admin rights OBS can’t raise its GPU priority, so a demanding game can starve OBS of GPU time, causing rendering lag.",
                                    "Right-click OBS → Properties → Compatibility → tick “Run this program as an administrator”."))
        if log.unclean_shutdown:
            findings.append(Finding("info", "OBS", "OBS did not close cleanly last time",
                                    "The log starts with “Crash or unclean shutdown detected”.",
                                    "Usually harmless, but repeated crashes can point to a plugin or driver problem.",
                                    "If it keeps happening, check Help → Crash Reports."))
        hdr_displays = [d for d in log.displays if "G2084" in d.get("space", "")]
        screen_kinds = {"monitor_capture", "window_capture", "game_capture"}
        t_rec = log.recordings[-1].start if log.recordings else None
        caps = log.sources_of_kind(screen_kinds, t_rec)
        rec_scenes = {o.scene for o in log.recordings if getattr(o, "scene", "")}
        used = [c for c in caps if c[0] in rec_scenes]
        if hdr_displays and caps and rec_scenes and not used:
            findings.append(Finding("info", "Color", "Windows HDR is on, but it didn’t affect this recording",
                                    ", ".join(f"{d.get('name')} ({d.get('size')}, {d.get('refresh')} Hz)" for d in hdr_displays) + " is running in HDR.",
                                    "HDR only matters for screen, window and game captures. The scene you recorded (" + ", ".join(f"“{x}”" for x in sorted(rec_scenes)) +
                                    ") has none, so nothing to change for it.",
                                    "If you record one of the scenes below, tick “Force SDR” on its capture source, or turn Windows HDR off while recording.",
                                    loc=[["Captures that would be affected", " · ".join(f"Scene “{sc}” → “{n}”" for sc, n, _k in caps[:6])]]))
            hdr_displays = []
        if hdr_displays:
            findings.append(Finding("info", "Color", "Windows HDR is turned on for a monitor",
                                    ", ".join(f"{d.get('name')} ({d.get('size')}, {d.get('refresh')} Hz)" for d in hdr_displays) + " is running in HDR (PQ/BT.2020).",
                                    "When the OBS canvas is SDR (Rec. 709), any Display/Window/Game Capture of an HDR screen must be tone-mapped. If “Force SDR” is off the result can look washed out or too dark.",
                                    "For SDR recordings of PC games: tick “Force SDR” on the capture source, or turn Windows HDR off while recording. Game Capture has no Force SDR box — use its Color Space setting instead. Capture-card sources are not affected.",
                                    loc=[["Captures in the scene you recorded", " · ".join(f"Scene “{sc}” → “{n}”" for sc, n, _k in used[:6])]] if used else
                                        ([["Screen captures in your scenes", " · ".join(f"Scene “{sc}” → “{n}”" for sc, n, _k in caps[:6])]] if caps else [])))
        if log.browser_errors >= 20:
            findings.append(Finding("info", "OBS", f"Browser sources logged {log.browser_errors} script errors",
                                    "Widgets (chat, alerts, ads) are throwing JavaScript errors.",
                                    "Mostly harmless, but busy or broken browser sources use CPU/GPU even when hidden.",
                                    "Remove widgets you don’t use, or enable “Shutdown source when not visible”."))
        ups = (log.recordings[0].start - log.first_ts).total_seconds() / 3600 if log.recordings and log.first_ts else 0
        if ups > 12:
            findings.append(Finding("info", "OBS", f"OBS had been open for {ups:.0f} hours before recording",
                                    f"The log starts {log.first_ts:%b %d %I:%M %p}; recording began {log.recordings[0].start:%b %d %I:%M %p}.",
                                    "Long OBS uptimes with browser sources can slowly build up memory/GPU use.",
                                    "Restart OBS before long recording sessions."))

    if log:
        k = len(findings) - n0
        st.done(f"{k} thing(s) to report." if k else "Nothing to report.", "found" if k else "ok")
    else:
        st.skip("Check OBS-wide settings", CHK_SYS, "Needs the OBS log.")

    # ---- per recording
    CHK_CMP = ("OBS settings against the file (resolution, frame rate, codec, bit depth, color format, range and space), capture devices against "
               "OBS's frame rate, encoder settings for the resolution, skipped and lagged frames, pauses, and the recording drive")
    CHK_TL = "Matches each skip, gap and OBS warning with what the PC was doing at that second, to find the likely cause"
    total_steps = max(len(recs), 1)
    for n, (idx, r) in enumerate(recs):
        o = r.output
        say(f"Analyzing {r.label}…", 0.05 + 0.6 * n / total_steps)
        path = (video_paths or {}).get(idx) or o.path
        if path:
            path = path.replace("/", os.sep) if sys.platform == "win32" else path
        st.begin("Find the recording file", "Looks for the video file where OBS saved it (or where you pointed the app to it)", r.label)
        if path and os.path.isfile(path):
            try:
                gb = os.path.getsize(path) / 1073741824
            except OSError:
                gb = 0
            st.done(f"Found {os.path.basename(path)} ({gb:.2f} GB).")

            def sub(msg, p, _n=n):
                say(f"{r.label}: {msg}", 0.05 + 0.6 * (_n + p) / total_steps)
            r.video = video_probe.probe(path, progress=sub, deep=deep, cancel=cancel, steps=st, rec=r.label)
        elif path:
            st.done(f"Not found at {os.path.basename(path)}, so only the OBS log was checked for this recording.", "failed")
            notes.append(f"{r.label}: file not found at {path} — only the OBS log was analyzed for it. Use “Locate file” if you moved it.")
        else:
            st.done("OBS didn't log a file name for this recording.", "skipped")
        if cancel and cancel():
            break

        stop = o.stop or (log.last_ts if log else None) or o.start
        r.samples = _window(samples, o.start - timedelta(seconds=2), stop + timedelta(seconds=2)) if samples else []
        letter = r.drive.get("letter", "")
        st.begin("Compare settings, file and frame counts", CHK_CMP, r.label)
        n0 = len(findings)
        _recording_findings(log, r, findings)
        k = len(findings) - n0
        st.done(f"{k} finding(s)." if k else "Everything matched.", "found" if k else "ok")
        st.begin("Build the timeline of problems", CHK_TL, r.label)
        n0 = len(incidents)
        _recording_incidents(log, r, incidents, letter)
        k = len(incidents) - n0
        st.done((f"{k} moment(s) on the timeline" + (", each with what the PC was doing." if r.samples else ".")) if k else "No problem moments.",
                "found" if k else "ok")

    # ---- Windows event log
    wev = []
    CHK_WEV = "Graphics driver resets and timeouts, disk and USB errors, and app crashes, from 2 minutes before to 2 minutes after recording"
    if recs:
        say("Checking Windows event log…", 0.7)
        st.begin("Check the Windows event log", CHK_WEV)
        n0 = len(incidents)
        try:
            t0 = min(r.output.start for _, r in recs) - timedelta(minutes=2)
            t1 = max((r.output.stop or r.output.start) for _, r in recs) + timedelta(minutes=2)
            wev = winevents.query(t0, t1)
        except Exception as e:
            notes.append(f"Could not read the Windows event log: {e}")
        for e in wev:
            if not e["meaning"] or "Update" in e["meaning"] or "Defender" in e["meaning"]:
                continue
            for _, r in recs:
                st = r.output.stop or r.output.start
                if r.output.start - timedelta(seconds=30) <= e["ts"] <= st + timedelta(seconds=30):
                    incidents.append(Incident(e["ts"], e["ts"], "winevent", f"Windows: {e['meaning']}",
                                              video_t=r.output.wall_to_video(e["ts"]),
                                              causes=[(f"{e['provider']} event {e['id']}: {e['message'][:160]}", "medium")],
                                              recording=r.label))
                    break
        k = len(incidents) - n0
        st.done(f"{len(wev)} event(s) read; {k} happened during a recording and are on the timeline." if wev else "No relevant Windows events.",
                "found" if k else "ok")

    # ---- monitor-only session (no OBS log)
    if not recs and samples:
        letter = ""
        rr = RecordingResult(output=obslog.Output(kind="recording", start=samples[0]["_dt"], stop=samples[-1]["_dt"]), label="Monitor session")
        rr.samples = samples
        _recording_incidents(None, rr, incidents, letter)
        recs.append((0, rr))

    if samples and not any(s.get("obs") for s in samples):
        notes.append("The monitor could not connect to OBS during this session, so exact skip times come only from the recording file. "
                     "Turn on Tools → WebSocket Server Settings → Enable WebSocket server in OBS.")

    incidents.sort(key=lambda i: i.start)
    findings = _merge_findings(findings, len(recs))
    findings.sort(key=lambda f: SEV_ORDER.get(f.severity, 9))
    say("Building report…", 0.95)
    st.begin("Write the report", "Combines repeated findings, sorts them by how serious they are, and adds the fixes")
    cnt = {k: sum(1 for f in findings if f.severity == k) for k in ("critical", "warning", "info")}
    st.done(f"{cnt['critical']} serious, {cnt['warning']} warning(s), {cnt['info']} note(s); {len(incidents)} timeline moment(s).")
    res = Result(created=datetime.now(), log=log, recordings=[r for _, r in recs], findings=findings,
                 incidents=incidents, winevents=wev, monitor_path=monitor_path or "", monitor_meta=meta,
                 samples=samples, marks=marks, notes=notes, steps=st.items)
    return res


def _merge_findings(findings, nrecs):
    """Combine identical findings that appear for several recordings."""
    merged, order = {}, []
    for f in findings:
        key = (f.severity, f.title)
        if key in merged:
            if f.recording and f.recording not in merged[key].recording:
                merged[key].recording += "; " + f.recording
        else:
            merged[key] = Finding(**f.__dict__)
            order.append(key)
    out = [merged[k] for k in order]
    for f in out:
        if nrecs > 1 and f.recording and f.recording.count(";") + 1 == nrecs:
            f.recording = "All recordings"
    return out


_CFG = None


def _cfg():
    """Current OBS settings on this PC (used only to name the right menus)."""
    global _CFG
    if _CFG is None:
        try:
            from . import obs_config
            _CFG = obs_config.load()
        except Exception:
            _CFG = False
    return _CFG or None


def _enc_loc(option: str) -> str:
    c = _cfg()
    if c is not None and getattr(c, "profile", ""):
        return c.enc_path(option)
    return f"OBS → Settings → Output → Recording → {option} (Output Mode: Advanced)"


def _src_loc(log, name: str, ts=None, kind_label: str = "") -> str:
    scenes = log.scenes_with(name, ts) if log else []
    where = (" and ".join(f"“{x}”" for x in scenes[:4]) + (f" (+{len(scenes) - 4} more)" if len(scenes) > 4 else "")) if scenes else ""
    src = f"source “{name}”" + (f" ({kind_label})" if kind_label else "")
    return (f"Scene {where} → {src}" if len(scenes) == 1 else f"Scenes {where} → {src}") if scenes else src[:1].upper() + src[1:]


def _recording_findings(log, r: RecordingResult, F: list):
    o = r.output
    lab = r.label
    vs = o.video_settings
    enc = o.encoder or {}
    v = r.video

    def add(sev, cat, title, detail, why="", fix="", loc=None):
        F.append(Finding(sev, cat, title, detail, why, fix, lab, [list(x) for x in (loc or [])]))

    rec_scene = getattr(o, "scene", "") or ""

    # --- skipped / lagged frames
    if o.encode_skipped is not None and o.encode_total:
        pct = 100 * o.encode_skipped / o.encode_total
        if o.encode_skipped == 0:
            add("good", "Performance", "No frames skipped due to encoding lag", f"0 of {o.encode_total:,} frames.")
        else:
            sev = "critical" if pct >= 1 else ("warning" if pct >= 0.05 else "info")
            heavy = []
            preset = str(enc.get("preset", "")).lower()
            from .obs_config import PRESET_LABELS
            mp_lbl = {"fullres": "Two Passes (Full Resolution)", "qres": "Two Passes (Quarter Resolution)"}
            if preset in ("p5", "p6", "p7", "slow", "quality", "max quality"):
                heavy.append(f"Preset {PRESET_LABELS.get(preset, enc.get('preset'))}")
            if str(enc.get("multipass", "")).lower() not in ("", "disabled", "false", "0"):
                heavy.append(f"Multipass Mode {mp_lbl.get(str(enc.get('multipass')).lower(), enc.get('multipass'))}")
            if str(enc.get("lookahead", "")).lower().startswith("true"):
                heavy.append("Look-ahead on")
            res = f"{enc.get('width', '')}x{enc.get('height', '')}" if enc.get("width") else (vs.output_res if vs else "")
            fps = vs.fps_value if vs else 0
            why = (f"The encoder ({enc.get('encoder', 'unknown')} {enc.get('codec', '')}, {res} @ {fps:g} fps) could not keep up, so OBS dropped "
                   f"frames before they reached the file. Common causes: GPU fully loaded by the game or OBS scene, encoder settings too heavy "
                   f"for the resolution/FPS, or the recording drive stalling.")
            fix = ("Check the incident timeline below to see what the PC was doing at each skip. "
                   + (f"Your encoder settings include {', '.join(heavy)}, which are demanding at {res}; try a faster preset. " if heavy else "")
                   + "If skips line up with high GPU load, cap the game’s FPS; if they line up with drive stalls, record to an internal SSD.")
            loc = [["Encoder", f"{enc.get('encoder', 'unknown')} {enc.get('codec', '')} at {res} {fps:g} fps".strip()]]
            if heavy:
                loc.append(["Heavy settings", ", ".join(heavy) + " — " + _enc_loc("Preset / Multipass Mode / Look-ahead")])
            if rec_scene:
                loc.append(["Scene recorded", f"“{rec_scene}”"])
            add(sev, "Performance", f"{o.encode_skipped:,} frames skipped due to encoding lag ({pct:.2f}%)",
                f"OBS reported {o.encode_skipped:,} of {o.encode_total:,} frames skipped.", why, fix, loc)
    if o.render_lagged:
        sev = "critical" if (o.render_lagged_pct or 0) >= 1 else "warning"
        add(sev, "Performance", f"{o.render_lagged:,} frames lagged due to rendering ({o.render_lagged_pct}%)",
            "OBS couldn’t finish drawing the scene in time for these frames.",
            "Almost always caused by the GPU being fully used (often by the game), or a very heavy scene (many browser sources, scaling filters).",
            "Cap the game’s FPS a little below your monitor refresh, run OBS as administrator, and remove unused sources.",
            [["Scene recorded", f"“{rec_scene}”" if rec_scene else "unknown"],
             ["Sources in it", ", ".join(f"“{n}”" for n, _k in (log.scenes_at(o.start).get(rec_scene, []) if log else [])[:8]) or "unknown"]])
    elif o.render_lagged == 0:
        add("good", "Performance", "No frames lagged due to rendering", "OBS rendered every frame on time.")

    # frame accounting: drawn vs output (+ pauses)
    if o.frames_drawn and o.frames_output is not None and vs and vs.fps_value:
        paused_frames = int(round(o.paused_seconds * vs.fps_value))
        missing = o.frames_drawn - o.frames_output - paused_frames
        if o.pauses:
            add("info", "Recording", f"Recording was paused {len(o.pauses)} time(s) ({fmt_t(o.paused_seconds)} total)",
                "Pauses: " + "; ".join(f"{p:%I:%M:%S %p}–{(u or o.stop):%I:%M:%S %p}" for p, u in o.pauses),
                "Times in this report are given both as clock time and as position in the video, with pauses removed.")
        if missing > max(120, o.frames_drawn * 0.002):
            add("warning", "Recording", f"About {missing:,} frames never reached the file",
                f"OBS drew {o.frames_drawn:,} frames and wrote {o.frames_output:,}"
                + (f" ({paused_frames:,} of the difference is from pauses)" if paused_frames else "") + ".",
                "Frames that are drawn but not output were dropped between OBS and the encoder.", "See the incident timeline for when this happened.")

    # --- canvas / output / encoder scaling
    if vs:
        b, out = _res(vs.base_res), _res(vs.output_res)
        if b and out and b != out:
            add("info", "Video settings", f"OBS scales the picture from {vs.base_res} to {vs.output_res}",
                f"Downscale filter: {vs.downscale}.",
                "Scaling costs GPU time each frame (Lanczos is the most expensive filter).",
                "If you see rendering lag, try Bicubic or set Base and Output resolution to the same value.",
                [["Setting", "OBS → Settings → Video → Base (Canvas) Resolution, Output (Scaled) Resolution, Downscale Filter"],
                 ["Now", f"{vs.base_res} → {vs.output_res}, {vs.downscale}"]])
        er = (int(enc["width"]), int(enc["height"])) if str(enc.get("width", "")).isdigit() and str(enc.get("height", "")).isdigit() else None
        if er and out and er != out:
            add("info", "Video settings", f"Recording encoder rescales to {er[0]}x{er[1]}",
                f"Output resolution is {vs.output_res}, but the recording encoder is set to {er[0]}x{er[1]}.",
                "An extra scaling pass on every frame.", "Untick “Rescale Output” unless you need a smaller recording than your Output resolution.",
                [["Setting", _enc_loc("Rescale Output")], ["Now", f"{er[0]}x{er[1]} (Output resolution {vs.output_res})"], ["Change to", "Unticked"]])
        # color
        fmt = (vs.format or "").upper()
        prof = str(enc.get("profile", "")).lower()
        tenbit_profile = "10" in prof or prof in ("main10", "high10")
        if fmt in ("NV12", "I420", "I444", "BGRA") and tenbit_profile:
            add("info", "Color", f"8-bit color format ({vs.format}) recorded with a 10-bit profile ({enc.get('profile')})",
                "OBS produces 8-bit pictures, but the encoder is told to use a 10-bit profile.",
                "This gives no 10-bit quality benefit — the file just stores 8-bit content in a 10-bit container — and some editors/players handle these files less smoothly.",
                "For normal (SDR) recording, set Profile to main. For real 10-bit, set Color Format to P010 instead (your sources must provide 10-bit/HDR).",
                [["Encoder setting", _enc_loc("Profile")], ["Now", f"Profile {enc.get('profile')}, Color Format {vs.format}"],
                 ["Color Format is in", "OBS → Settings → Advanced → Video → Color Format"], ["Change to", "Profile: main (keep Color Format NV12)"]])
        if fmt in ("P010", "I010") and "2100" not in (vs.color_space or "") and not tenbit_profile and enc:
            add("warning", "Color", "10-bit color format with an 8-bit encoder profile",
                f"Color format {vs.format} but encoder profile is {enc.get('profile')}.",
                "The encoder will throw away the extra precision or fail.", "Use profile main10 (HEVC) or a 10-bit AV1 setting with P010.",
                [["Encoder setting", _enc_loc("Profile")], ["Color Format is in", "OBS → Settings → Advanced → Video → Color Format"]])
        if "2100" in (vs.color_space or "") and fmt not in ("P010", "I010"):
            add("critical", "Color", "HDR color space with an 8-bit color format",
                f"Color space {vs.color_space} with format {vs.format}.",
                "HDR (Rec. 2100) needs a 10-bit format; otherwise colors are wrong.", "Set Color Format to P010, or Color Space back to Rec. 709.",
                [["Setting", "OBS → Settings → Advanced → Video → Color Format and Color Space"], ["Now", f"{vs.format}, {vs.color_space}"]])
        if (vs.color_range or "").lower() == "full":
            add("warning", "Color", "Color range is set to Full",
                f"OBS color settings: {vs.yuv_mode}.",
                "Most players, editors and YouTube expect Limited range video. Full-range files are often shown with crushed blacks/blown highlights or look washed out, depending on the app.",
                "Set it to Limited, unless you know your whole editing chain handles Full.",
                [["Setting", "OBS → Settings → Advanced → Video → Color Range"], ["Now", "Full"], ["Change to", "Limited"]])

    # --- capture devices
    canvas_fps = vs.fps_value if vs else 0
    for d in o.devices:
        dname = f"“{d.name}” ({d.device})" if d.device else f"“{d.name}”"
        dloc = ["Source", _src_loc(log, d.name, o.start, d.device or "Video Capture Device")]
        if canvas_fps and d.fps:
            ratio = d.fps / canvas_fps
            if abs(d.fps - canvas_fps) < 0.001:
                pass
            elif abs(ratio - round(ratio)) < 0.002 and round(ratio) >= 2:
                add("info", "Frame rate", f"{dname} runs at {d.fps:g} fps but OBS records at {canvas_fps:g} fps",
                    "OBS keeps every other frame.", "Works fine, but uses extra USB bandwidth and CPU for frames that are thrown away.",
                    f"Optional: double-click the source → Resolution/FPS Type: Custom → FPS: {canvas_fps:g} (or “Match Output FPS”).",
                    [dloc, ["Device sends", f"{d.fps:g} fps"], ["OBS records", f"{canvas_fps:g} fps (OBS → Settings → Video → Common FPS Values)"]])
            else:
                period = 1 / abs(d.fps - canvas_fps) if abs(d.fps - canvas_fps) > 0 else 0
                add("warning", "Frame rate", f"Frame-rate mismatch: {dname} {d.fps:g} fps vs OBS {canvas_fps:g} fps",
                    f"The source and OBS run at slightly different rates.",
                    f"OBS has to repeat or drop a frame about every {period:.1f} seconds, which shows up as a regular micro-stutter." if period else "",
                    f"Make both the same. Easiest: double-click the source → Resolution/FPS Type: Custom → FPS: {canvas_fps:g}. "
                    f"If the device only offers {d.fps:g}, set OBS → Settings → Video → Common FPS Values to {d.fps:g} instead. "
                    f"If the device itself is set to {d.fps:g} (for example a console's video output), changing it there also works.",
                    [dloc, ["Device sends", f"{d.fps:g} fps (source property: FPS)"], ["OBS records", f"{canvas_fps:g} fps (OBS → Settings → Video → Common FPS Values)"],
                     ["Change to", f"FPS {canvas_fps:g} on the source"]])
        b = _res(vs.base_res) if vs else None
        dr = _res(d.resolution)
        if b and dr and dr != b:
            add("info", "Video settings", f"{dname} is {d.resolution}, canvas is {vs.base_res}",
                "OBS scales this source to fit the canvas.",
                "Fine if intended; upscaling adds no detail and costs GPU time.", "Match the device resolution to the canvas when possible.",
                [dloc, ["Device sends", f"{d.resolution} (source property: Resolution)"], ["Canvas", f"{vs.base_res} (OBS → Settings → Video → Base (Canvas) Resolution)"]])
        # Only relevant when this source's audio actually misbehaved (OBS kept adding buffering for it).
        late = (getattr(log, "audio_buffering_sources", []) or []).count(d.name) if log else 0
        if d.sample_rate and log and log.audio_rate and d.sample_rate != log.audio_rate and late >= 2:
            from .live import audio_rate_fix
            add("warning", "Audio", f"Audio from {dname} arrived late {late} times and runs at {d.sample_rate} Hz (OBS: {log.audio_rate} Hz)",
                "OBS kept adding audio buffering for this source.",
                "OBS converts the rate in real time; on its own that’s harmless, but combined with repeated late audio it can cause drift or sync offsets.",
                " ".join(audio_rate_fix(d, log.audio_rate)),
                [dloc, ["Source audio", f"{d.sample_rate} Hz"], ["OBS audio", f"{log.audio_rate} Hz (OBS → Settings → Audio → Sample Rate)"]])
        if d.format and d.format.upper() in ("MJPEG", "MJPG") and (d.hw_decode or "").lower().startswith("disabled"):
            add("info", "Capture", f"{dname} sends MJPEG with hardware decode off",
                "The CPU has to decompress every frame.", "Costs CPU and can drop frames at high resolutions.",
                "Double-click the source → Video Format: NV12 or YUY2 if offered. Otherwise tick “Use hardware decoding when available”.",
                [dloc, ["Now", f"Video Format {d.format}, hardware decoding off"]])

    # --- encoder settings sanity
    if enc:
        e = str(enc.get("encoder", "")).lower()
        if "nvenc" in e:
            if str(enc.get("lookahead", "")).lower().startswith("true") and vs and vs.fps_value >= 60 and _res(vs.output_res) and _res(vs.output_res)[0] >= 3840:
                add("warning", "Encoder", "Look-ahead is on for 4K60 NVENC",
                    "Look-ahead adds significant encoder work.", "At 4K60 this can push NVENC past its limit.", "Turn Look-ahead off.",
                    [["Setting", _enc_loc("Look-ahead")], ["Now", "On"], ["Change to", "Off"]])
            mp = str(enc.get("multipass", "")).lower()
            if mp in ("fullres", "full resolution", "2"):
                add("warning", "Encoder", "Two-pass (full resolution) is on",
                    f"Multipass: {enc.get('multipass')}.", "Doubles the encoder workload at high resolutions.", "Use Two Passes (Quarter Resolution) or Single Pass.",
                    [["Setting", _enc_loc("Multipass Mode")], ["Now", "Two Passes (Full Resolution)"], ["Change to", "Two Passes (Quarter Resolution)"]])
        if "x264" in e and vs and _res(vs.output_res) and _res(vs.output_res)[0] >= 2560:
            add("warning", "Encoder", "CPU encoding (x264) at high resolution",
                f"x264 at {vs.output_res}.", "Software encoding 1440p/4K is extremely CPU heavy and a common cause of encoding lag.",
                "Switch to your graphics card’s hardware encoder (NVIDIA NVENC, AMD AMF or Intel QuickSync).",
                [["Setting", _enc_loc("Video Encoder")], ["Now", "x264 (CPU)"]])
    path = o.path or ""
    if path.lower().endswith((".mp4", ".mov")) and "hybrid" not in str(o.encoder).lower():
        add("info", "Recording", "Recording directly to MP4/MOV",
            "If OBS, the PC or the drive crashes, an MP4 file can be unreadable.",
            "MKV (or Hybrid MP4 in OBS 30.2+) survives crashes.", "Choose Hybrid MP4 (if listed) or Matroska Video (.mkv).",
            [["Setting", _enc_loc("Recording Format")], ["Now", os.path.splitext(path)[1].upper().lstrip(".")]])

    # --- drive
    dv = r.drive
    if dv.get("type") == "network":
        add("warning", "Storage", f"Recording to a network drive ({dv.get('letter') or path[:2]})",
            "Network shares can pause for a moment at any time.", "Any pause blocks OBS’s file writer and can cause skipped frames.", "Record to a local SSD and move files afterwards.")
    elif dv.get("bus", "").upper() == "USB":
        add("info", "Storage", f"Recording drive {dv.get('letter')} is a USB {dv.get('media') or 'drive'} ({dv.get('name')})",
            "External USB drives and enclosures can stall briefly (power saving, spin-down, controller hiccups).",
            "A stall of even a fraction of a second can back up OBS. If incidents line up with drive stalls, this is the likely cause.",
            "Prefer an internal SSD. Otherwise: Control Panel → Power Options → Change plan settings → Change advanced power settings → "
            "Hard disk → Turn off hard disk after → Never, and USB settings → USB selective suspend setting → Disabled.")
    elif (dv.get("media") or "").upper() == "HDD":
        add("info", "Storage", f"Recording drive {dv.get('letter')} is a hard disk ({dv.get('name')})",
            "Hard disks are fine for typical recording bitrates but slow down when other programs use them at the same time.", "",
            "Avoid using this drive for other things while recording (game installs, downloads, backups, antivirus scans).",
            [["Recording path", _enc_loc("Recording Path")]])

    # --- recording file checks
    if v and v.ok:
        vv = v.video
        if vs:
            out = _res(vs.output_res)
            enc_res = (int(enc["width"]), int(enc["height"])) if str(enc.get("width", "")).isdigit() else out
            if enc_res and (vv.get("width"), vv.get("height")) != enc_res:
                add("info", "File", f"File resolution {vv.get('width')}x{vv.get('height')} differs from OBS output {enc_res[0]}x{enc_res[1]}",
                    "The file isn't the size OBS was set to make.", "Usually means settings changed after this recording, or the file was converted afterwards.",
                    "If you didn't change anything, check the two settings below.",
                    [["Output resolution", "OBS → Settings → Video → Output (Scaled) Resolution"], ["Rescale Output", _enc_loc("Rescale Output")]])
            if vs.fps_value and v.nominal_fps and abs(v.nominal_fps - vs.fps_value) > 0.01:
                add("warning", "File", f"File frame rate {v.nominal_fps:.3f} differs from OBS {vs.fps_value:g}",
                    "The frame rate stored in the file doesn't match OBS.", "Players/editors may misjudge timing.",
                    "If you didn't change the frame rate after this recording, set it again and make a short test recording.",
                    [["Setting", "OBS → Settings → Video → Common FPS Values"], ["OBS", f"{vs.fps_value:g}"], ["File", f"{v.nominal_fps:.3f}"]])
            fr = obslog.range_ui(vv.get("color_range", ""))
            orng = obslog.range_ui(vs.color_range)
            if fr in ("Full", "Limited") and orng in ("Full", "Limited") and fr != orng:
                now = _cfg()
                now_rng = obslog.range_ui(getattr(now, "color_range", "")) if now is not None and getattr(now, "profile", "") else ""
                changed = bool(now_rng) and now_rng != orng
                add("warning", "Color", f"File is marked {fr} range, but OBS was set to {orng}",
                    f"OBS recorded with Color Range {orng} (from the OBS log), but the file says {fr}.",
                    "Players and editors read the file's marking. If it's wrong, blacks look grey or shadows look crushed.",
                    ("Your Color Range has been changed since this recording, so new recordings will match. Nothing else to do."
                     if changed else
                     "OBS normally writes its own setting into the file, so this points to a custom encoder setting or a converted/remuxed file. "
                     "If you use Recording Type: Custom Output (FFmpeg), remove any color_range option from its muxer/encoder settings. "
                     "If you converted the file (HandBrake, an editor), check that tool's color range option."),
                    [["OBS setting", "OBS → Settings → Advanced → Video → Color Range"], ["At recording time", orng],
                     ["Now", now_rng or "unknown"], ["In the file", fr]])
            cs = vv.get("color_space", "unknown")
            if cs in ("unknown", "", None):
                add("info", "Color", "Color space is not marked in the file",
                    "The file doesn't say which color standard (BT.601 or BT.709) it uses.",
                    "Players have to guess. For HD and 4K they almost always guess BT.709, which matches OBS, so this is usually harmless.",
                    "Nothing to change in OBS. If colors look slightly off in an editor, set the clip's color space to Rec. 709 there.",
                    [["OBS setting", f"OBS → Settings → Advanced → Video → Color Space (now {vs.color_space or 'unknown'})"]])
            elif "709" in (vs.color_space or "") and "709" not in cs:
                add("warning", "Color", f"File color space {cs} doesn’t match OBS setting {vs.color_space}",
                    "The file is marked with a different color standard than OBS used.", "Colors may shift in players/editors.",
                    "If you didn't change Color Space after this recording, check for a custom encoder setting or a converted file.",
                    [["OBS setting", "OBS → Settings → Advanced → Video → Color Space"], ["OBS", vs.color_space], ["File", cs]])
        if v.vfr:
            add("warning", "File", "Variable frame timing detected",
                f"Frame durations vary through the file (measured {v.measured_fps:.3f} fps vs nominal {v.nominal_fps:.3f}).",
                "Editors such as Premiere and Vegas can drift audio out of sync on variable-frame-rate files.",
                "Re-encode/convert to constant frame rate before editing, or fix the skips causing it.")
        if v.gaps:
            obs_skips = (o.encode_skipped or 0) + (o.render_lagged or 0)
            steps = ["Open the report's timeline: each gap is listed with the time in the video and what the PC was doing at that moment."]
            loc = [["When", ", ".join(f"{fmt_t(g.t)}" for g in v.gaps[:6]) + (f" (+{len(v.gaps) - 6} more)" if len(v.gaps) > 6 else "") + " into the video"]]
            if obs_skips:
                steps.append("OBS also reported skipped/lagged frames, so fix those first (see the encoding/rendering findings).")
            if o.devices:
                d = o.devices[0]
                loc.append(["Likely source", _src_loc(log, d.name, o.start, d.device or "Video Capture Device")])
                steps.append("If OBS reported no skips at those moments, the frames never arrived from the capture device. Plug it straight into a USB 3 port "
                             "on the back of the PC (not a hub or front port), and in the source's properties set Buffering to Enable.")
            if (r.drive.get("bus") or "").upper() == "USB" or (r.drive.get("media") or "").upper() == "HDD":
                steps.append(f"The recording drive {r.drive.get('letter', '')} is a {'USB' if (r.drive.get('bus') or '').upper() == 'USB' else 'hard'} drive; "
                             "if the timeline shows it busy or slow at those moments, record to an internal SSD.")
            add("critical" if v.gap_frames_total > 60 else "warning", "File",
                f"{len(v.gaps)} timestamp gap(s) in the file — {v.gap_frames_total:,} missing frames",
                "Moments where the next frame comes later than it should; see the timeline for each one.",
                "These are visible stutters/skips in the video.", " ".join(steps), loc)
        elif v.frame_count:
            add("good", "File", "No missing frames found in the file",
                f"{v.frame_count:,} frames checked; every timestamp is where it should be.")
        if v.backwards_ts:
            add("warning", "File", f"{v.backwards_ts} timestamp(s) go backwards", "Some frames are stamped earlier than the frame before them.",
                "Can cause playback/editing glitches.", "Remux the file in OBS: File → Remux Recordings, pick this file, click Remux.")
        if v.audio_gaps:
            asrc = [x for x in dict.fromkeys(getattr(log, "audio_buffering_sources", []) or [])] if log else []
            loc = [["When", ", ".join(fmt_t(g["t"]) for g in v.audio_gaps[:6]) + " into the video"]]
            if asrc:
                loc.append(["Source OBS had trouble with", " · ".join(_src_loc(log, n, o.start) for n in asrc[:3])])
            add("warning", "Audio", f"{len(v.audio_gaps)} gap(s) in the audio track", "Short stretches where the audio track has no sound data.",
                "Audible dropouts / pops.",
                ("OBS had to add audio buffering for the source listed below, so its audio arrived late. Open the source's properties and check its audio settings, "
                 "and make sure OBS → Settings → Audio → Sample Rate matches the device (48 kHz is standard)."
                 if asrc else
                 "Check the timeline at those times for high CPU or drive activity. If nothing stands out, the audio device itself dropped out; reconnect it or update its driver."),
                loc)
        if abs(v.av_drift_ms) >= 100:
            add("warning", "Audio", f"Audio and video lengths differ by {v.av_drift_ms/1000:.2f} s",
                f"Audio ends {abs(v.av_drift_ms):.0f} ms {'after' if v.av_drift_ms > 0 else 'before'} the video.",
                "Points to audio drift or dropped video frames.",
                ("Fix the missing video frames first; the audio usually lines up once frames stop being dropped."
                 if (v.gaps or o.encode_skipped) else
                 "Check that OBS → Settings → Audio → Sample Rate matches your audio devices (48 kHz is standard). If the drift grows over long recordings, "
                 "open Edit → Advanced Audio Properties and check the Sync Offset of each source."),
                [["Audio sources", ", ".join(f"“{x}”" for x in list(dict.fromkeys(getattr(log, "audio_buffering_sources", []) or []))[:3]) or "see OBS → Edit → Advanced Audio Properties"]])
        if v.deep_scanned:
            if v.dup_periodic_s:
                add("warning", "Frame rate", f"Regular repeated frame every ~{v.dup_periodic_s} s",
                    f"{len(v.dup_isolated)} isolated repeated frames at a steady interval.",
                    "This is the signature of a frame-rate mismatch between source and OBS (e.g. 59.94 vs 60).",
                    "Set the source and OBS to exactly the same FPS (see the frame-rate finding for the exact source and values).",
                    [["Repeats every", f"about {v.dup_periodic_s} s"]] + ([["Likely source", _src_loc(log, o.devices[0].name, o.start, o.devices[0].device or "Video Capture Device")]] if o.devices else []))
            if v.dup_isolated and not v.dup_periodic_s:
                add("info", "File", f"{len(v.dup_isolated)} isolated repeated frames",
                    "Single frames shown twice during motion — usually what an encoding-lag skip looks like in the finished file.",
                    "Usually caused by the same thing as the skipped frames; fix those and these go away.",
                    "If there are no encoding skips, see the timeline for what the PC was doing at those times.")
            if v.dup_runs:
                long_runs = [d for d in v.dup_runs if d.frames >= 30]
                add("info", "File", f"{len(v.dup_runs)} frozen stretches ({len(long_runs)} longer than 0.5 s)",
                    "Several identical frames in a row. Can be a real freeze, or just a static scene (pause menu, loading screen).",
                    "Freezes during gameplay are worth a look; freezes during menus or loading screens are normal.",
                    "Jump to the times in the timeline. If the game was moving there, the capture source stopped sending frames — check its cable and USB port.")
    if v and not v.ok and v.error:
        add("info", "File", "Recording file could not be checked", v.error,
            "Without the file, only the OBS log and PC data were analyzed.",
            "Make sure the recording has finished and the file isn't open in another program, then analyze again. If you moved the file, use Locate file.")
    _ = log


def _recording_incidents(log, r: RecordingResult, I: list, letter: str):
    o = r.output
    rows = r.samples
    lab = r.label
    # OBS live skips from the monitor
    rp, ep = [], []
    for s in rows:
        ob = s.get("obs") or {}
        if ob.get("d_render"):
            rp.append((s["_dt"], ob["d_render"]))
        if ob.get("d_enc") and ob.get("rec"):
            ep.append((s["_dt"], ob["d_enc"]))
    for kind, pts, title in (("render", rp, "Frames missed — rendering lag"), ("encode", ep, "Frames skipped — encoding lag")):
        for st, en, frames in _cluster(pts):
            win = _window(rows, st - timedelta(seconds=3), en + timedelta(seconds=1))
            m = metrics_at(win, letter)
            I.append(Incident(st - timedelta(seconds=1), en, kind, title, frames=frames, video_t=o.wall_to_video(st - timedelta(seconds=1)),
                              metrics=m, causes=explain(kind, m, r.drive), recording=lab))
    # file timestamp gaps
    v = r.video
    if v and v.ok:
        gpts = [(o.video_to_wall(g.t), g.missing, g.t) for g in v.gaps]
        groups = []
        for w, nmiss, t in sorted(gpts):
            if groups and (w - groups[-1][1]).total_seconds() <= 2:
                groups[-1][1] = w
                groups[-1][2] += nmiss
            else:
                groups.append([w, w, nmiss, t])
        for st, en, nmiss, t in groups[:500]:
            win = _window(rows, st - timedelta(seconds=3), en + timedelta(seconds=2))
            m = metrics_at(win, letter) if win else {}
            # if this lines up with an OBS-reported incident, merge
            merged = False
            for inc in I:
                if inc.recording == lab and inc.kind in ("render", "encode") and abs((inc.start - st).total_seconds()) <= 4:
                    inc.title += f" + {nmiss} frames missing in file"
                    merged = True
                    break
            if not merged:
                I.append(Incident(st, en, "file-gap", f"{nmiss} frame(s) missing in the file", frames=nmiss, video_t=t,
                                  metrics=m, causes=explain("file-gap", m, r.drive), recording=lab))
        for ag in v.audio_gaps[:200]:
            w = o.video_to_wall(ag["t"])
            I.append(Incident(w, w, "audio-gap", f"Audio gap of {ag['length_ms']:.0f} ms", video_t=ag["t"], recording=lab,
                              causes=[("Audio packets missing — audio device hiccup or audio buffering change", "medium")]))
        if v.deep_scanned and not v.dup_periodic_s:
            # cluster isolated repeats into bursts, which are the interesting ones
            bursts = []
            for t in v.dup_isolated:
                if bursts and t - bursts[-1][1] <= 3:
                    bursts[-1][1] = t
                    bursts[-1][2] += 1
                else:
                    bursts.append([t, t, 1])
            for st_t, en_t, cnt in bursts:
                if cnt < 3:
                    continue
                w = o.video_to_wall(st_t)
                win = _window(rows, w - timedelta(seconds=3), o.video_to_wall(en_t) + timedelta(seconds=2))
                m = metrics_at(win, letter) if win else {}
                merged = False
                for inc in I:
                    if inc.recording == lab and inc.kind in ("render", "encode") and abs((inc.start - w).total_seconds()) <= 4:
                        inc.title += f" (seen as {cnt} repeated frames in the file)"
                        merged = True
                        break
                if not merged:
                    I.append(Incident(w, o.video_to_wall(en_t), "repeat", f"Burst of {cnt} repeated frames (stutter)", frames=cnt,
                                      video_t=st_t, metrics=m, causes=explain("repeat", m, r.drive), recording=lab))
            for d in v.dup_runs:
                if d.frames >= 20:
                    w = o.video_to_wall(d.t)
                    I.append(Incident(w, w + timedelta(seconds=d.frames / (v.nominal_fps or 60)), "freeze",
                                      f"Picture frozen for {d.frames} frames ({d.frames / (v.nominal_fps or 60):.1f} s)",
                                      frames=d.frames, video_t=d.t, recording=lab,
                                      causes=[("Static scene (menu/loading screen), or the capture source stopped sending new frames", "low")]))
    # log warnings during the recording
    if log:
        stop = o.stop or log.last_ts
        for e in log.events:
            if o.start <= e.ts <= stop and e.severity in ("warning", "error") and e.category not in ("audio-device",):
                cause = []
                if "audio buffering" in e.text:
                    src = re.search(r"source: (.*)\)", e.text)
                    cause.append((f"OBS increased audio buffering because {src.group(1) if src else 'a source'} delivered audio late — can cause a short audio/video desync", "high"))
                elif e.category == "capture":
                    cause.append(("Capture device configuration/signal problem — the device may have lost signal or changed format", "medium"))
                elif e.category == "encoder":
                    cause.append(("Encoder error", "high"))
                elif e.category == "disk":
                    cause.append(("File writing problem", "high"))
                elif e.category == "gpu":
                    cause.append(("GPU driver problem", "high"))
                I.append(Incident(e.ts, e.ts, "log", f"OBS log: {e.text[:140]}", video_t=o.wall_to_video(e.ts), causes=cause, recording=lab))
        for p, u in o.pauses:
            I.append(Incident(p, u or p, "pause", f"Recording paused for {fmt_t(((u or p) - p).total_seconds())}",
                              video_t=o.wall_to_video(p - timedelta(milliseconds=1)), recording=lab))
    # severe system spikes even without skips (risk indicators)
    if rows and letter:
        spikes = [s for s in rows if ((s.get("disks") or {}).get(letter) or {}).get("write_ms", 0) >= 500]
        for st, en, n in _cluster([(s["_dt"], 1) for s in spikes], gap_s=5)[:50]:
            if any(abs((i.start - st).total_seconds()) < 6 for i in I if i.recording == lab):
                continue
            win = _window(rows, st - timedelta(seconds=1), en + timedelta(seconds=1))
            m = metrics_at(win, letter)
            I.append(Incident(st, en, "spike", f"Recording drive {letter} stalled ({m.get('disk_write_ms') or 0:.0f} ms writes) — no skip reported",
                              video_t=o.wall_to_video(st), metrics=m, recording=lab,
                              causes=[("Drive stall that OBS absorbed this time; longer ones can cause skips", "low")]))

    # summary stats
    if rows:
        def series(key, sub=None):
            out = []
            for s in rows:
                val = s.get(key)
                if sub:
                    val = (val or {}).get(sub) if isinstance(val, dict) else None
                if isinstance(val, (int, float)):
                    out.append(val)
            return out

        def summ(vals):
            if not vals:
                return None
            vs_ = sorted(vals)
            return {"avg": round(sum(vals) / len(vals), 1), "max": round(vs_[-1], 1), "p95": round(vs_[int(len(vs_) * 0.95) - 1 if len(vs_) > 1 else 0], 1)}
        disk_busy = [((s.get("disks") or {}).get(letter) or {}).get("busy") for s in rows] if letter else []
        disk_ms = [((s.get("disks") or {}).get(letter) or {}).get("write_ms") for s in rows] if letter else []
        r.stats = {
            "cpu": summ(series("cpu")), "gpu_3d": summ(series("gpu_3d")), "gpu_enc": summ(series("gpu_enc")),
            "ram": summ(series("ram")), "disk_busy": summ([x for x in disk_busy if isinstance(x, (int, float))]),
            "disk_write_ms": summ([x for x in disk_ms if isinstance(x, (int, float))]),
            "nv_temp": summ([s["nv"]["temp"] for s in rows if s.get("nv") and isinstance(s["nv"].get("temp"), (int, float))]),
            "obs_render_ms": summ([s["obs"]["render_ms"] for s in rows if s.get("obs")]),
            "samples": len(rows),
            "obs_connected": sum(1 for s in rows if s.get("obs")),
        }
