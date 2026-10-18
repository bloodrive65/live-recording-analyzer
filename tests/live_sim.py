import os, sys, time, json
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analyzer import live, obs_log
class FakeMon:
    obs_status = "connected"; latest = None
mon = FakeMon()
adv = live.LiveAdvisor(mon)
adv.preflight(announce=False); adv._open_log(seek_end=True)
from datetime import timedelta
T0=datetime.now()
logp = obs_log.latest_logs(1)[0]
def logline(t):
    with open(logp, 'a') as f: f.write(datetime.now().strftime("%H:%M:%S.000") + ": " + t + "\n")
es = 0; rb = 0
for i in range(40):
    rec = 2 <= i <= 34
    s = {"ts": (T0+timedelta(seconds=i)).isoformat(timespec="milliseconds"), "cpu": 20, "cpu_core_max": 50, "ram": 50, "gpu_3d": 40, "gpu_enc": 70,
         "disks": {"Z:": {"busy": 10, "write_ms": 8}}, "procs": {"cpu": [["obs64.exe", 8]], "io": []}}
    o = {"fps": 60, "render_ms": 3, "render_skip": 0, "enc_skip": es, "rec": rec, "rec_paused": False, "rec_ms": max(0, i-2)*1000,
         "rec_bytes": rb, "rec_mbs": 19, "disk_free_mb": 900000}
    if rec and not (24 <= i <= 31): rb += 20_000_000
    o["rec_bytes"] = rb
    if i == 2: logline("[ffmpeg muxer: 'adv_file_output'] Writing file 'Z:/[RECORDINGS]/gaps.mkv'...")
    if i in (6, 7, 8):
        s["disks"]["Z:"] = {"busy": 100, "write_ms": 1300, "queue": 8}; es += 4; o["enc_skip"] = es; o["d_enc"] = 4
    if 12 <= i <= 22: s["gpu_enc"] = 99
    if i == 14: logline("adding 64 milliseconds of audio buffering, total audio buffering is now 277 milliseconds (source: Video Capture Device)")
    if i == 34: logline("Video stopped, number of skipped frames due to encoding lag: 12/2000 (0.6%)")
    s["obs"] = o; s["type"] = "sample"
    mon.latest = s
    # speed: advisor loop runs every 1s; force sample handling
    s2 = dict(s); s2["_dt"] = datetime.fromisoformat(s["ts"]); adv.hist.append(s2); adv._on_sample(s2); adv._tail_log()
for x in live.quick_file_check('/tmp/gaps.mkv', adv.cfg): adv.post(x)
for a in adv.drain():
    if a.phase == "before": continue
    print(f"{a.severity:8} {a.phase:6} {'UPD' if a.update else '   '} x{a.count} | {a.title} | {a.body[:90]}")
print("want_analysis", adv.want_analysis)
