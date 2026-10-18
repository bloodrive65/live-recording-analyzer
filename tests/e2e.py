import json, sys, os
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analyzer import analyze, report
T = datetime(2026, 10, 18, 14, 0, 0)
def ts(d): return d.strftime("%H:%M:%S.") + f"{d.microsecond//1000:03d}"
src = open('/tmp/sample.txt', encoding='utf-8', errors='replace').read().splitlines()
head = [l for l in src[:160]]
log = []
for l in head:
    log.append(ts(T - timedelta(minutes=5)) + l[12:])
log[0:0] = [ts(T - timedelta(minutes=5)) + ": Current Date/Time: 2026-10-18, 13:55:00"]
L = lambda d, t: log.append(f"{ts(d)}: {t}")
L(T - timedelta(seconds=60), "[DShow Device: 'Video Capture Device'] settings updated: ")
for t in ["\tvideo device: Elgato 4K X", "\tresolution: 3840x2160", "\tfps: 59.94 (interval: 166833)", "\tformat: NV12", "\tbuffering: disabled", "\tsample rate: 48000"]:
    L(T - timedelta(seconds=60), t)
L(T - timedelta(seconds=1), "[obs-nvenc: 'advanced_video_recording'] settings:")
for t in ["\tcodec:        HEVC", "\trate_control: CQP", "\tcqp:          21", "\tpreset:       p6", "\tmultipass:    fullres", "\tprofile:      main10", "\twidth:        640", "\theight:       360", "\tlookahead:    false (0 frames)"]:
    L(T - timedelta(seconds=1), t)
L(T, "==== Recording Start ===============================================")
L(T, "[ffmpeg muxer: 'adv_file_output'] Writing file 'Z:/[RECORDINGS]/test.mkv'...")
L(T + timedelta(seconds=8), "adding 64 milliseconds of audio buffering, total audio buffering is now 277 milliseconds (source: Video Capture Device)")
L(T + timedelta(seconds=30), "[ffmpeg muxer: 'adv_file_output'] Output of file 'Z:/[RECORDINGS]/test.mkv' stopped")
L(T + timedelta(seconds=30), "Output 'adv_file_output': Total frames output: 1792")
L(T + timedelta(seconds=30), "Output 'adv_file_output': Total drawn frames: 1800")
L(T + timedelta(seconds=30), "Video stopped, number of skipped frames due to encoding lag: 8/1800 (0.4%)")
L(T + timedelta(seconds=30), "==== Recording Stop ================================================")
open('/tmp/test-log.txt', 'w').write("\n".join(log) + "\n")
# monitor
with open('/tmp/monitor.jsonl', 'w') as f:
    f.write(json.dumps({"type": "session_start", "ts": (T - timedelta(seconds=10)).isoformat()}) + "\n")
    rs, es = 0, 0
    for i in range(-10, 40):
        d = T + timedelta(seconds=i)
        s = {"type": "sample", "ts": d.isoformat(timespec="milliseconds"), "cpu": 22, "cpu_core_max": 60, "ram": 48,
             "gpu_3d": 45, "gpu_enc": 70, "disks": {"Z:": {"busy": 12, "write_ms": 9, "write_mbs": 20, "queue": 0.1}},
             "nv": {"temp": 66, "enc": 68}, "procs": {"cpu": [["obs64.exe", 9]], "io": [["obs64.exe", 20, 0]]},
             "gpu_procs": [["obs64.exe", 40]]}
        o = {"fps": 60, "render_ms": 3.1, "render_skip": rs, "enc_skip": es, "rec": 0 <= i <= 30, "rec_tc": ""}
        if i == 5:
            s["disks"]["Z:"] = {"busy": 100, "write_ms": 1450, "write_mbs": 0.3, "queue": 9}
            es += 6; o["enc_skip"] = es; o["d_enc"] = 6
            s["procs"]["io"].append(["SearchIndexer.exe", 85, 0])
        if i == 15:
            s["gpu_enc"] = 99; s["nv"]["enc"] = 100
            es += 2; o["enc_skip"] = es; o["d_enc"] = 2
        if i == 22:
            s["gpu_3d"] = 99; rs += 3; o["render_skip"] = rs; o["d_render"] = 3
            s["procs"]["cpu"].append(["MsMpEng.exe", 31])
        if i == 35:
            s["disks"]["Z:"]["write_ms"] = 800
        s["obs"] = o
        f.write(json.dumps(s) + "\n")
res = analyze.run('/tmp/test-log.txt', video_paths={0: '/tmp/gaps.mkv'}, monitor_path='/tmp/monitor.jsonl', deep=True)
for fnd in res.findings: print(fnd.severity, '|', fnd.title)
for i in res.incidents: print(i.start.time(), analyze.fmt_t(i.video_t), i.kind, i.title, [c for c in i.causes])
p = report.save(res, '/tmp/reports'); print(p)
