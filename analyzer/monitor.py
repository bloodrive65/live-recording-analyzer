"""Background session monitor.

Samples once per second while you play and writes a JSON-lines file:
CPU, RAM, GPU engines (3D / video encode / decode), every drive's busy %,
write speed and latency, network, top processes, and OBS's own live stats
(rendering + encoding skips) through OBS WebSocket.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Optional

import psutil

from .obsws import ObsClient, obs_ws_config

IS_WIN = sys.platform == "win32"
# no console window + below-normal priority, so helper programs never compete with the game or OBS
CREATE_NO_WINDOW = (0x08000000 | 0x00004000) if IS_WIN else 0

try:
    import win32pdh  # type: ignore
except Exception:  # pragma: no cover - not on Windows
    win32pdh = None


ENG_RE = re.compile(r"pid_(\d+)_luid_(0x[0-9A-Fa-f]+_0x[0-9A-Fa-f]+)_phys_(\d+)_eng_(\d+)_engtype_(.*)$")
IGNORE_PROCS = {"System Idle Process", "Idle", "System", "Registry", "Memory Compression"}


class Pdh:
    """Windows performance counters with wildcard re-expansion."""

    def __init__(self, patterns):
        self.patterns = patterns
        self.q = None
        self.counters = {}
        self.last_expand = 0
        self.ok = win32pdh is not None
        self.primed = False

    def _expand(self):
        if self.q:
            try:
                win32pdh.CloseQuery(self.q)
            except Exception:
                pass
        self.q = win32pdh.OpenQuery()
        self.counters = {}
        for pat in self.patterns:
            try:
                paths = win32pdh.ExpandCounterPath(pat)
            except Exception:
                continue
            for p in paths:
                try:
                    self.counters[p] = win32pdh.AddCounter(self.q, p)
                except Exception:
                    pass
        self.last_expand = time.time()
        try:
            win32pdh.CollectQueryData(self.q)
        except Exception:
            pass
        self.primed = False

    def read(self, reexpand_every=15.0):
        if not self.ok:
            return {}
        if self.q is None or time.time() - self.last_expand > reexpand_every:
            self._expand()
            time.sleep(0.05)
        try:
            win32pdh.CollectQueryData(self.q)
        except Exception:
            return {}
        out = {}
        for p, h in self.counters.items():
            try:
                _, val = win32pdh.GetFormattedCounterValue(h, win32pdh.PDH_FMT_DOUBLE | 0x00008000)  # NOCAP100
                out[p] = val
            except Exception:
                continue
        return out


def _inst(path: str) -> str:
    m = re.search(r"\((.*)\)", path)
    return m.group(1) if m else ""


class NvidiaSmi:
    """Long-running nvidia-smi reader (temperature, clocks, power, encoder %)."""

    FIELDS_FULL = "timestamp,name,temperature.gpu,power.draw,clocks.gr,clocks.mem,utilization.gpu,utilization.encoder,memory.used,memory.total,clocks_throttle_reasons.active"
    FIELDS_BASIC = "timestamp,name,temperature.gpu,power.draw,clocks.gr,clocks.mem,utilization.gpu,memory.used,memory.total"

    def __init__(self):
        self.latest = {}
        self.proc = None
        exe = "nvidia-smi"
        for c in (r"C:\Windows\System32\nvidia-smi.exe", r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
            if os.path.isfile(c):
                exe = c
        self.exe = exe

    def start(self):
        for fields in (self.FIELDS_FULL, self.FIELDS_BASIC):
            try:
                test = subprocess.run([self.exe, f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
                                      capture_output=True, text=True, timeout=8, creationflags=CREATE_NO_WINDOW)
            except Exception:
                return False
            if test.returncode == 0 and "Field" not in test.stdout and test.stdout.strip():
                self.fields = fields.split(",")
                self.proc = subprocess.Popen([self.exe, f"--query-gpu={fields}", "--format=csv,noheader,nounits", "-lms", "1000"],
                                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                             creationflags=CREATE_NO_WINDOW)
                threading.Thread(target=self._reader, daemon=True).start()
                return True
        return False

    def _reader(self):
        for line in self.proc.stdout:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != len(self.fields):
                continue
            d = {}
            for k, v in zip(self.fields, parts):
                if k in ("timestamp", "name", "clocks_throttle_reasons.active"):
                    d[k] = v
                else:
                    try:
                        d[k] = float(v)
                    except ValueError:
                        d[k] = None
            self.latest = d

    def stop(self):
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass


class Monitor:
    def __init__(self, out_dir: str, interval: float = 1.0, obs_host="127.0.0.1", obs_port: Optional[int] = None,
                 obs_password: Optional[str] = None, use_obs=True, light: bool = False):
        os.makedirs(out_dir, exist_ok=True)
        # Light mode: sample every 2 s, look at other programs every 10 s, refresh GPU counter list less often
        self.light = light
        if light:
            interval = max(interval, 2.0)
        self.proc_every = 10.0 if light else 5.0
        self.pdh_reexpand = 30.0 if light else 15.0
        self.out_dir = out_dir
        self.path = os.path.join(out_dir, "monitor.jsonl")
        self.interval = interval
        self.stop_evt = threading.Event()
        self.thread = None
        self.latest = {}
        self.status = "stopped"
        self.obs_status = "not connected"
        self.counts = {"render_skips": 0, "encode_skips": 0, "samples": 0, "events": 0}
        cfg = obs_ws_config()
        self.obs_host = obs_host
        self.obs_port = obs_port or cfg["port"]
        self.obs_password = obs_password if obs_password is not None else cfg["password"]
        self.use_obs = use_obs
        self.obs = None
        self._last_obs_try = 0
        self._prev_obs = None
        self._prev_disk = None
        self._prev_net = None
        self._proc_cache = {}
        self._last_proc = 0
        self.pdh = Pdh([
            r"\GPU Engine(*)\Utilization Percentage",
            r"\GPU Adapter Memory(*)\Dedicated Usage",
            r"\LogicalDisk(*)\% Idle Time",
            r"\LogicalDisk(*)\Avg. Disk sec/Write",
            r"\LogicalDisk(*)\Disk Write Bytes/sec",
            r"\LogicalDisk(*)\Current Disk Queue Length",
            r"\LogicalDisk(*)\Free Megabytes",
            r"\SMB Client Shares(*)\Avg. sec/Write",
            r"\SMB Client Shares(*)\Write Bytes/sec",
            r"\Processor Information(_Total)\% Processor Performance",
        ]) if IS_WIN else None
        self.nv = NvidiaSmi() if IS_WIN else None
        self._fh = None

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        self.stop_evt.clear()
        self._fh = open(self.path, "a", encoding="utf-8")
        self._write({"type": "session_start", "ts": datetime.now().isoformat(timespec="milliseconds"),
                     "host": os.environ.get("COMPUTERNAME", ""), "cpu_count": psutil.cpu_count(),
                     "ram_total": psutil.virtual_memory().total, "drives": self._drives()})
        if self.nv:
            try:
                self.nv.start()
            except Exception:
                pass
        psutil.cpu_percent(percpu=True)
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        self.status = "running"

    def stop(self):
        self.stop_evt.set()
        if self.thread:
            self.thread.join(timeout=5)
        if self.nv:
            self.nv.stop()
        if self.obs:
            self.obs.close()
        self._write({"type": "session_stop", "ts": datetime.now().isoformat(timespec="milliseconds")})
        if self._fh:
            self._fh.close()
            self._fh = None
        self.status = "stopped"

    def _write(self, obj):
        if self._fh:
            self._fh.write(json.dumps(obj, separators=(",", ":")) + "\n")
            self._fh.flush()

    def _drives(self):
        out = []
        for p in psutil.disk_partitions(all=True):
            d = {"device": p.device, "mount": p.mountpoint, "fstype": p.fstype, "opts": p.opts}
            if IS_WIN:
                try:
                    import ctypes
                    t = ctypes.windll.kernel32.GetDriveTypeW(p.mountpoint)
                    d["type"] = {2: "removable", 3: "fixed", 4: "network", 5: "cdrom", 6: "ramdisk"}.get(t, "unknown")
                except Exception:
                    pass
            out.append(d)
        return out

    # ------------------------------------------------------------------ sampling
    def _loop(self):
        if sys.platform == "win32":     # stay out of the game's and OBS's way (only this thread, not the window)
            try:
                import ctypes
                k = ctypes.windll.kernel32
                k.SetThreadPriority(k.GetCurrentThread(), -1)   # THREAD_PRIORITY_BELOW_NORMAL
            except Exception:
                pass
        next_t = time.time()
        while not self.stop_evt.is_set():
            try:
                s = self.sample()
                self.latest = s
                self._write(s)
                self.counts["samples"] += 1
            except Exception as e:  # keep running no matter what
                self._write({"type": "error", "ts": datetime.now().isoformat(), "error": repr(e)})
            next_t += self.interval
            delay = next_t - time.time()
            if delay < 0:
                next_t = time.time()
                delay = 0
            self.stop_evt.wait(delay)

    def sample(self) -> dict:
        now = datetime.now()
        s = {"type": "sample", "ts": now.isoformat(timespec="milliseconds")}
        cores = psutil.cpu_percent(percpu=True)
        s["cpu"] = round(sum(cores) / len(cores), 1) if cores else None
        s["cpu_core_max"] = max(cores) if cores else None
        vm = psutil.virtual_memory()
        s["ram"] = vm.percent
        try:
            sw = psutil.swap_memory()
            s["pagefile"] = sw.percent
        except Exception:
            pass

        # --- Windows performance counters
        if self.pdh:
            vals = self.pdh.read(reexpand_every=self.pdh_reexpand)
            eng = defaultdict(float)
            per_pid_3d = defaultdict(float)
            per_pid_enc = defaultdict(float)
            disks = defaultdict(dict)
            smb = defaultdict(dict)
            vram = {}
            for path, val in vals.items():
                inst = _inst(path)
                low = path.lower()
                if "\\gpu engine(" in low:
                    m = ENG_RE.match(inst)
                    if not m:
                        continue
                    pid, luid, phys, engno, etype = m.groups()
                    etype = etype.strip()
                    eng[(luid, phys, engno, etype)] += val
                    if etype == "3D":
                        per_pid_3d[int(pid)] += val
                    elif "VideoEncode" in etype or etype.startswith("Encode"):
                        per_pid_enc[int(pid)] += val
                elif "\\gpu adapter memory(" in low:
                    vram[inst] = round(val / 1048576)
                elif "\\logicaldisk(" in low:
                    if inst == "_Total" or "HarddiskVolume" in inst:
                        continue
                    if low.endswith("% idle time"):
                        disks[inst]["busy"] = round(max(0.0, 100 - min(val, 100)), 1)
                    elif low.endswith("avg. disk sec/write"):
                        disks[inst]["write_ms"] = round(val * 1000, 1)
                    elif low.endswith("disk write bytes/sec"):
                        disks[inst]["write_mbs"] = round(val / 1048576, 1)
                    elif low.endswith("current disk queue length"):
                        disks[inst]["queue"] = round(val, 1)
                    elif low.endswith("free megabytes"):
                        disks[inst]["free_gb"] = round(val / 1024, 1)
                elif "\\smb client shares(" in low:
                    if low.endswith("avg. sec/write"):
                        smb[inst]["write_ms"] = round(val * 1000, 1)
                    elif low.endswith("write bytes/sec"):
                        smb[inst]["write_mbs"] = round(val / 1048576, 1)
                elif "% processor performance" in low:
                    s["cpu_perf_pct"] = round(val, 1)
            # Task-Manager style: per engine sum across processes, then max per type
            by_type = defaultdict(float)
            for (luid, phys, engno, etype), v in eng.items():
                key = "3d" if etype == "3D" else ("enc" if ("Encode" in etype) else ("dec" if "Decode" in etype else ("copy" if etype == "Copy" else None)))
                if key:
                    by_type[key] = max(by_type[key], min(v, 100.0))
            for k in ("3d", "enc", "dec", "copy"):
                s["gpu_" + k] = round(by_type.get(k, 0.0), 1) if eng else None
            if per_pid_3d:
                top = sorted(per_pid_3d.items(), key=lambda x: -x[1])[:3]
                s["gpu_procs"] = [[self._pname(pid), round(v, 1)] for pid, v in top if v >= 1]
            if per_pid_enc:
                top = sorted(per_pid_enc.items(), key=lambda x: -x[1])[:2]
                s["enc_procs"] = [[self._pname(pid), round(v, 1)] for pid, v in top if v >= 1]
            if vram:
                s["vram_mb"] = max(vram.values())
            if disks:
                s["disks"] = dict(disks)
            if smb:
                s["smb"] = dict(smb)
        else:
            # Non-Windows fallback (used for testing): psutil disk counters
            try:
                io = psutil.disk_io_counters(perdisk=True)
                t = time.time()
                if self._prev_disk:
                    pt, pio = self._prev_disk
                    dt = max(t - pt, 0.001)
                    disks = {}
                    for k, v in io.items():
                        if k in pio:
                            busy = getattr(v, "busy_time", 0) - getattr(pio[k], "busy_time", 0)
                            disks[k] = {"busy": round(min(100, busy / (dt * 10)), 1),
                                        "write_mbs": round((v.write_bytes - pio[k].write_bytes) / dt / 1048576, 1)}
                    s["disks"] = disks
                self._prev_disk = (t, io)
            except Exception:
                pass

        # --- nvidia-smi extras
        if self.nv and self.nv.latest:
            n = self.nv.latest
            s["nv"] = {"temp": n.get("temperature.gpu"), "power": n.get("power.draw"), "clock": n.get("clocks.gr"),
                       "util": n.get("utilization.gpu"), "enc": n.get("utilization.encoder"),
                       "mem": n.get("memory.used"), "mem_total": n.get("memory.total"),
                       "throttle": n.get("clocks_throttle_reasons.active")}

        # --- network
        try:
            net = psutil.net_io_counters()
            t = time.time()
            if self._prev_net:
                pt, pn = self._prev_net
                dt = max(t - pt, 0.001)
                s["net_up_mbs"] = round((net.bytes_sent - pn.bytes_sent) / dt / 1048576, 2)
                s["net_down_mbs"] = round((net.bytes_recv - pn.bytes_recv) / dt / 1048576, 2)
            self._prev_net = (t, net)
        except Exception:
            pass

        # --- busiest programs (every 5 s, or 10 s in Light mode)
        if time.time() - self._last_proc >= self.proc_every:
            self._last_proc = time.time()
            s["procs"] = self._top_procs()

        # --- OBS live stats
        if self.use_obs:
            self._obs_sample(s)
        return s

    def _pname(self, pid):
        try:
            return psutil.Process(pid).name()
        except Exception:
            return f"pid {pid}"

    def _top_procs(self):
        ncpu = psutil.cpu_count() or 1
        rows = []
        seen = set()
        for p in psutil.process_iter(["pid", "name"]):
            pid = p.info["pid"]
            seen.add(pid)
            name = p.info.get("name") or ""
            if name in IGNORE_PROCS or pid == 0:
                continue
            try:
                ct = p.cpu_times()
                cpu_total = ct.user + ct.system
                try:
                    io = p.io_counters()
                    wb, rb = io.write_bytes, io.read_bytes
                except Exception:
                    wb = rb = 0
                mem = p.memory_info().rss
            except Exception:
                continue
            t = time.time()
            prev = self._proc_cache.get(pid)
            self._proc_cache[pid] = (t, cpu_total, wb, rb)
            if not prev:
                continue
            dt = max(t - prev[0], 0.001)
            cpu = (cpu_total - prev[1]) / dt * 100 / ncpu
            wmb = (wb - prev[2]) / dt / 1048576
            rmb = (rb - prev[3]) / dt / 1048576
            rows.append((name, round(cpu, 1), round(wmb, 1), round(rmb, 1), round(mem / 1048576)))
        for pid in list(self._proc_cache):
            if pid not in seen:
                del self._proc_cache[pid]
        by_cpu = [r for r in sorted(rows, key=lambda r: -r[1])[:6] if r[1] >= 1]
        by_io = [r for r in sorted(rows, key=lambda r: -(r[2] + r[3]))[:4] if r[2] + r[3] >= 1]
        return {"cpu": [[r[0], r[1]] for r in by_cpu], "io": [[r[0], r[2], r[3]] for r in by_io]}

    def _obs_sample(self, s):
        if self.obs is None:
            if time.time() - self._last_obs_try < 10:
                s["obs"] = None
                return
            self._last_obs_try = time.time()
            try:
                c = ObsClient(self.obs_host, self.obs_port, self.obs_password)
                c.connect()
                self.obs = c
                self.obs_status = "connected"
                self._write({"type": "obs_connected", "ts": s["ts"]})
            except PermissionError as e:
                self.obs_status = "password rejected – check OBS WebSocket password"
                s["obs"] = None
                return
            except Exception:
                self.obs_status = "OBS not reachable (is OBS open with WebSocket enabled?)"
                s["obs"] = None
                return
        try:
            st = self.obs.request("GetStats")
            rec = self.obs.request("GetRecordStatus")
            try:
                stream = self.obs.request("GetStreamStatus")
            except Exception:
                stream = {}
        except Exception:
            self.obs.close()
            self.obs = None
            self.obs_status = "connection lost – retrying"
            self._write({"type": "obs_disconnected", "ts": s["ts"]})
            s["obs"] = None
            return
        o = {
            "fps": round(st.get("activeFps", 0), 2),
            "render_ms": round(st.get("averageFrameRenderTime", 0), 2),
            "render_skip": st.get("renderSkippedFrames", 0),
            "render_total": st.get("renderTotalFrames", 0),
            "enc_skip": st.get("outputSkippedFrames", 0),
            "enc_total": st.get("outputTotalFrames", 0),
            "obs_cpu": round(st.get("cpuUsage", 0), 1),
            "obs_mem": round(st.get("memoryUsage", 0)),
            "disk_free_mb": round(st.get("availableDiskSpace", 0)),
            "rec": bool(rec.get("outputActive")),
            "rec_paused": bool(rec.get("outputPaused")),
            "rec_tc": rec.get("outputTimecode", ""),
            "rec_ms": rec.get("outputDuration", 0),
            "rec_bytes": rec.get("outputBytes", 0),
        }
        if stream.get("outputActive"):
            o["stream"] = True
            o["stream_skip"] = stream.get("outputSkippedFrames", 0)
            o["stream_congestion"] = round(stream.get("outputCongestion", 0) or 0, 3)
            o["stream_reconnecting"] = stream.get("outputReconnecting", False)
        p = self._prev_obs
        if p:
            dr = o["render_skip"] - p["render_skip"]
            de = o["enc_skip"] - p["enc_skip"]
            if dr > 0:
                o["d_render"] = dr
                self.counts["render_skips"] += dr
            if de > 0:
                o["d_enc"] = de
                self.counts["encode_skips"] += de
            if o["rec"] != p["rec"]:
                self._write({"type": "record_on" if o["rec"] else "record_off", "ts": s["ts"]})
            if o["rec_bytes"] and p.get("rec_bytes") is not None and o["rec"] and p["rec"]:
                o["rec_mbs"] = round((o["rec_bytes"] - p["rec_bytes"]) / 1048576 / self.interval, 1)
        self._prev_obs = o
        for et, data in self.obs.pop_events():
            self._write({"type": "obs_event", "ts": s["ts"], "event": et, "data": data})
            self.counts["events"] += 1
        s["obs"] = o


def load_session(path: str):
    meta, samples, marks = {}, [], []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("type")
            if t == "sample":
                samples.append(d)
            elif t == "session_start":
                if not meta:
                    meta = d
            else:
                marks.append(d)
    for s in samples:
        s["_dt"] = datetime.fromisoformat(s["ts"])
    for m in marks:
        try:
            m["_dt"] = datetime.fromisoformat(m["ts"])
        except Exception:
            pass
    return meta, samples, marks
