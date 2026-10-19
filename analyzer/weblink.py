"""Turn an analysis into a link to the online report page.

The whole report is compressed into the part of the link after “#”. Browsers never send that part to a
server, so GitHub only serves the empty report page — the report itself stays in the link. The link works
on any device (phone, another PC) and can be bookmarked or shared.
"""
from __future__ import annotations

import base64
import json
import os
import zlib

from .analyze import Result, fmt_t

REPORT_PAGE = "https://bloodrive65.github.io/live-recording-analyzer/report.html"
SCHEMA = 1


def _num(v, nd=0):
    if not isinstance(v, (int, float)):
        return None
    return round(v, nd) if nd else int(round(v))


def _metrics_text(m) -> str:
    if not m:
        return ""
    bits = []
    for k, lab, unit in (("gpu_3d", "GPU", "%"), ("gpu_enc", "Encoder", "%"), ("nv_enc", "NVENC", "%"), ("cpu", "CPU", "%"),
                         ("disk_busy", "Drive", "% busy"), ("disk_write_ms", "Write", " ms"), ("ram", "RAM", "%"),
                         ("nv_temp", "GPU temp", "°C"), ("obs_render_ms", "OBS render", " ms")):
        v = m.get(k)
        if isinstance(v, (int, float)):
            bits.append(f"{lab} {v:.0f}{unit}")
    if m.get("top_cpu"):
        bits.append("Busiest: " + ", ".join(f"{n} {v:.0f}%" for n, v in m["top_cpu"][:3]))
    return " · ".join(bits)


def _chart(rec, letter, incidents, points=320):
    rows = rec.samples
    if len(rows) < 3:
        return None
    t0, t1 = rows[0]["_dt"], rows[-1]["_dt"]
    span = max((t1 - t0).total_seconds(), 1)
    step = max(1, len(rows) // points)
    series = {"gpu": [], "enc": [], "cpu": [], "disk": []}
    xs = []
    for i in range(0, len(rows), step):
        chunk = rows[i:i + step]
        xs.append(int((chunk[0]["_dt"] - t0).total_seconds()))

        def mx(get):
            vals = [v for v in (get(r) for r in chunk) if isinstance(v, (int, float))]
            return int(round(max(vals))) if vals else None
        series["gpu"].append(mx(lambda r: r.get("gpu_3d")))
        series["enc"].append(mx(lambda r: r.get("gpu_enc") if r.get("gpu_enc") is not None else r.get("nv_enc")))
        series["cpu"].append(mx(lambda r: r.get("cpu")))
        series["disk"].append(mx(lambda r: ((r.get("disks") or {}).get(letter) or {}).get("busy")))
    marks = []
    for inc in incidents:
        if t0 <= inc.start <= t1 and inc.kind != "pause":
            marks.append([int((inc.start - t0).total_seconds()), inc.kind, inc.title[:80]])
    return {"t0": t0.isoformat(timespec="seconds"), "span": int(span), "x": xs,
            "s": {k: v for k, v in series.items() if any(x is not None for x in v)}, "m": marks[:300]}


def to_data(res: Result, version: str = "", compare=None) -> dict:
    log = res.log
    crit = [f for f in res.findings if f.severity == "critical"]
    warn = [f for f in res.findings if f.severity == "warning"]
    total_render = sum((r.output.render_lagged or 0) for r in res.recordings)
    total_enc = sum((r.output.encode_skipped or 0) for r in res.recordings)
    live_render = sum(i.frames for i in res.incidents if i.kind == "render")
    live_enc = sum(i.frames for i in res.incidents if i.kind == "encode")
    vids = [r for r in res.recordings if r.video and r.video.ok]
    file_missing = sum(r.video.gap_frames_total for r in vids)
    if crit:
        verdict = ["crit", "Problems found", f"{len(crit)} serious issue(s) and {len(warn)} warning(s). Most important: {crit[0].title}."]
    elif warn:
        verdict = ["warn", f"Mostly OK, with {len(warn)} thing(s) worth fixing", f"Top item: {warn[0].title}."]
    else:
        verdict = ["good", "Clean recording", "No skipped frames or setting conflicts were found."]
    d = {
        "v": SCHEMA, "app": version, "created": res.created.isoformat(timespec="seconds"),
        "verdict": verdict,
        "cards": [["Rendering lag", max(total_render, live_render)], ["Encoding skips", max(total_enc, live_enc)],
                  ["Missing frames in file", file_missing if vids else None],
                  ["Timeline incidents", len([i for i in res.incidents if i.kind != "pause"])]],
        "sources": [], "notes": list(res.notes),
        "findings": [{"s": f.severity, "c": f.category, "t": f.title, "d": f.detail, "w": f.why, "f": f.fix,
                      **({"l": f.loc} if getattr(f, "loc", None) else {})} for f in res.findings],
        "recs": [], "win": [], "sys": [],
    }
    if log:
        d["sources"].append(f"OBS log ({log.obs_version})")
    if res.monitor_path:
        d["sources"].append(f"Watching data: {len(res.samples):,} samples")
    if vids:
        d["sources"].append("Recording file checked" + (" (deep scan)" if any(r.video.deep_scanned for r in vids) else ""))
    if compare:
        d["compare"] = {"t": compare[0], "l": compare[1]}

    for r in res.recordings:
        o = r.output
        kv = []
        if o.path:
            kv.append(["File", os.path.basename(o.path)])     # file name only — no folders in a shareable link
        kv.append(["Started", f"{o.start:%b %d %Y, %I:%M:%S %p}"])
        if o.stop:
            kv.append(["Length", fmt_t(o.duration_seconds - o.paused_seconds)])
        if r.drive.get("letter"):
            dv = r.drive
            dtxt = " ".join(x for x in (dv.get("letter"), dv.get("name"), f"({dv.get('bus')} {dv.get('media')})" if dv.get("bus") else "") if x)
            if dtxt:
                kv.append(["Recording drive", dtxt])
        if o.encode_total:
            kv.append(["Encoding skips", f"{o.encode_skipped:,} of {o.encode_total:,} frames ({100 * o.encode_skipped / o.encode_total:.2f}%)"])
        if o.render_lagged is not None:
            kv.append(["Rendering lag", f"{o.render_lagged:,} frames ({o.render_lagged_pct}%)"])
        enc = o.encoder or {}
        if enc:
            kv.append(["Encoder", " · ".join(f"{k} {enc[k]}" for k in ("encoder", "codec", "rate_control", "cqp", "cq", "bitrate", "preset",
                                                                       "tuning", "multipass", "profile", "lookahead") if k in enc)])
        vs, v = o.video_settings, r.video
        settings = []
        if vs or (v and v.ok):
            vv = (v.video if v and v.ok else {}) or {}

            def row(k, a, b, ok=None):
                if str(b).lower() in ("unknown", "none", "—", "") or str(a).lower() in ("unknown", "—", ""):
                    ok = None
                elif ok is None:
                    ok = str(a).lower().replace(" ", "") == str(b).lower().replace(" ", "")
                settings.append([k, str(a), str(b), ok])
            if vs:
                eres = f"{enc.get('width')}x{enc.get('height')}" if enc.get("width") else vs.output_res
                row("Resolution", eres, f"{vv.get('width')}x{vv.get('height')}" if vv else "—")
                row("Frame rate", f"{vs.fps_value:g}", (f"{v.nominal_fps:.3f}".rstrip("0").rstrip(".")) if vv else "—")
                rng = {"pc": "Full", "jpeg": "Full", "tv": "Limited", "mpeg": "Limited"}.get(vv.get("color_range", ""), vv.get("color_range", "—")) if vv else "—"
                row("Color range", vs.color_range or "—", rng)
                cs_map = {"bt709": "Rec. 709", "bt470bg": "Rec. 601", "smpte170m": "Rec. 601", "bt2020nc": "Rec. 2100", "bt2020c": "Rec. 2100"}
                row("Color space", vs.color_space or "—", cs_map.get(vv.get("color_space", ""), vv.get("color_space", "—")) if vv else "—")
            if v and v.ok:
                kv.append(["File details", f"{v.container} · {v.duration / 60:.1f} min · {v.size_bytes / 1073741824:.1f} GiB · {v.bitrate / 1e6:.0f} Mb/s · "
                                           f"{v.frame_count:,} frames (expected {v.expected_frames:,})"])
        stats = []
        for k, lab, unit in (("gpu_3d", "GPU 3D load", "%"), ("gpu_enc", "GPU video encoder", "%"), ("cpu", "CPU", "%"), ("ram", "RAM", "%"),
                             ("disk_busy", "Recording drive busy", "%"), ("disk_write_ms", "Drive write latency", " ms"),
                             ("nv_temp", "GPU temperature", " °C"), ("obs_render_ms", "OBS frame render time", " ms")):
            s = (r.stats or {}).get(k)
            if s:
                stats.append([lab, s["avg"], s["p95"], s["max"], unit])
        incs = [i for i in res.incidents if i.recording == r.label and i.kind != "pause"]
        inc_rows = [[f"{i.start:%I:%M:%S %p}", fmt_t(i.video_t) if i.video_t is not None else "", i.kind, i.title,
                     _metrics_text(i.metrics) if i.kind not in ("log", "winevent", "audio-gap", "freeze") else "",
                     [[t, c] for t, c in (i.causes or [])][:3]] for i in incs[:180]]
        d["recs"].append({"label": r.label, "kv": kv, "settings": settings, "stats": stats,
                          "chart": _chart(r, r.drive.get("letter", ""), incs) if r.stats else None,
                          "inc": inc_rows, "more": max(0, len(incs) - 180),
                          "obs_live": bool((r.stats or {}).get("obs_connected"))})
    for e in [e for e in res.winevents if e.get("meaning")][:60]:
        d["win"].append([f"{e['ts']:%I:%M:%S %p}", f"{e['provider']} #{e['id']}", e["meaning"], (e.get("message") or "")[:160]])
    if log:
        s = log.system
        for k, lab in (("cpu", "CPU"), ("memory", "Memory"), ("windows", "Windows"), ("obs_gpu", "OBS renders on"), ("game_mode", "Game Mode")):
            if s.get(k):
                d["sys"].append([lab, str(s[k])])
        for a in log.adapters:
            d["sys"].append(["GPU", f"{a['name']} · driver {a.get('driver', '')}"])
        for dsp in log.displays:
            d["sys"].append(["Display", f"{dsp.get('size', '')} @ {dsp.get('refresh', '')} Hz · {dsp.get('space', '')}"])
        if log.audio_rate:
            d["sys"].append(["OBS audio", f"{log.audio_rate} Hz"])
    return d


def encode(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    c = zlib.compressobj(9, zlib.DEFLATED, -15)          # raw deflate: the page uses DecompressionStream("deflate-raw")
    packed = c.compress(raw) + c.flush()
    return base64.urlsafe_b64encode(packed).decode("ascii").rstrip("=")


def decode(token: str) -> dict:
    pad = "=" * (-len(token) % 4)
    return json.loads(zlib.decompress(base64.urlsafe_b64decode(token + pad), -15).decode("utf-8"))


def link(data: dict) -> str:
    return f"{REPORT_PAGE}#r={encode(data)}"
