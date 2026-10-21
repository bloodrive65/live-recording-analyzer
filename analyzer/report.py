"""Self-contained HTML report."""
from __future__ import annotations

import html
import json
import os
from datetime import datetime

from .analyze import Result, fmt_t, SEV_ORDER

E = html.escape

CSS = """
:root{--bg:#141416;--panel:#1c1c20;--panel2:#222228;--line:#2c2c33;--text:#e7e9ee;--muted:#9a9ca5;
--crit:#ff5d5d;--warn:#f5b748;--info:#5fa8ff;--good:#3ecf8e;--accent:#e7e9ee}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 "Segoe UI",system-ui,-apple-system,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 22px 60px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:34px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:16px;margin:18px 0 8px}.muted{color:var(--muted)}.small{font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:18px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .k{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}.card .v{font-size:24px;font-weight:650;margin-top:4px;font-variant-numeric:tabular-nums}
.verdict{border-radius:12px;padding:16px 18px;margin:16px 0;border:1px solid var(--line);background:var(--panel)}
.verdict.crit{border-left:5px solid var(--crit)}.verdict.warn{border-left:5px solid var(--warn)}.verdict.good{border-left:5px solid var(--good)}
.f{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:10px 0}
.where{margin:8px 0 10px;padding:8px 10px;border-left:3px solid #5fa8ff;border-radius:6px;background:rgba(95,168,255,.07)}.where .wh{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:#8fc1ff;margin-bottom:4px}.where dl{display:grid;grid-template-columns:max-content 1fr;gap:3px 14px;margin:0}.where dt{color:#9a9ca5}.where dd{margin:0}
.f summary{cursor:pointer;list-style:none;display:flex;gap:10px;align-items:baseline}.f summary::-webkit-details-marker{display:none}
.f .t{font-weight:600}.f .rec{margin-left:auto;font-size:12px;color:var(--muted);text-align:right}
.f .body{margin-top:8px;color:#cfd4dd}.f .body b{color:var(--text)}
.badge{display:inline-block;font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}
.b-critical{background:rgba(255,93,93,.15);color:var(--crit)}.b-warning{background:rgba(245,183,72,.15);color:var(--warn)}
.b-info{background:rgba(95,168,255,.15);color:var(--info)}.b-good{background:rgba(62,207,142,.15);color:var(--good)}
.b-high{background:rgba(255,93,93,.15);color:var(--crit)}.b-medium{background:rgba(245,183,72,.15);color:var(--warn)}.b-low{background:rgba(154,163,178,.15);color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:14px;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{background:var(--panel2);font-weight:600;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
tr:last-child td{border-bottom:none}td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
.mismatch{color:var(--warn);font-weight:600}.match{color:var(--good)}
.chart{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:10px 0}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:13px;color:var(--muted);margin:4px 4px 6px}.legend i{display:inline-block;width:12px;height:3px;margin-right:6px;vertical-align:middle;border-radius:2px}
ul.c{margin:4px 0;padding-left:18px}ul.c li{margin:2px 0}
.note{background:var(--panel2);border:1px dashed var(--line);border-radius:10px;padding:10px 14px;margin:8px 0;color:var(--muted)}
.kv{display:grid;grid-template-columns:220px 1fr;gap:4px 14px;font-size:14px}.kv div:nth-child(odd){color:var(--muted)}
footer{margin-top:40px;color:var(--muted);font-size:12px}
"""

SERIES = [
    ("gpu_3d", "GPU 3D load %", "#ff6b6b"),
    ("gpu_enc", "GPU video encoder %", "#f5b748"),
    ("cpu", "CPU %", "#5fa8ff"),
    ("disk_busy", "Recording drive busy %", "#3ecf8e"),
]


def _svg_chart(rec, letter, incidents, width=1120, height=230):
    rows = rec.samples
    if len(rows) < 3:
        return ""
    t0, t1 = rows[0]["_dt"], rows[-1]["_dt"]
    span = max((t1 - t0).total_seconds(), 1)
    pad_l, pad_r, pad_t, pad_b = 34, 8, 8, 24
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    step = max(1, len(rows) // 1400)

    def x(dt):
        return pad_l + w * (dt - t0).total_seconds() / span

    def y(v):
        return pad_t + h * (1 - min(max(v, 0), 100) / 100)

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="System load over time">']
    for g in (0, 25, 50, 75, 100):
        parts.append(f'<line x1="{pad_l}" x2="{width-pad_r}" y1="{y(g):.1f}" y2="{y(g):.1f}" stroke="#2c2c33" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l-6}" y="{y(g)+4:.1f}" fill="#9a9ca5" font-size="11" text-anchor="end">{g}</text>')
    # time ticks
    tf = "%I:%M:%S %p" if span < 900 else "%I:%M %p"
    for k in range(7):
        dt = t0 + (t1 - t0) * (k / 6)
        anchor = "start" if k == 0 else ("end" if k == 6 else "middle")
        parts.append(f'<text x="{x(dt):.1f}" y="{height-6}" fill="#9a9ca5" font-size="11" text-anchor="{anchor}">{dt.strftime(tf)}</text>')
    # incident markers
    for inc in incidents:
        if inc.kind in ("render", "encode", "file-gap", "repeat", "freeze", "winevent", "audio-gap") and t0 <= inc.start <= t1:
            col = {"render": "#ff5d5d", "encode": "#ff5d5d", "file-gap": "#ff9b5d", "repeat": "#ff9b5d", "winevent": "#c58bff"}.get(inc.kind, "#9a9ca5")
            xx = x(inc.start)
            parts.append(f'<line x1="{xx:.1f}" x2="{xx:.1f}" y1="{pad_t}" y2="{pad_t+h}" stroke="{col}" stroke-opacity=".55" stroke-width="1.5"><title>{E(inc.start.strftime("%I:%M:%S %p"))} {E(inc.title)}</title></line>')
        elif inc.kind == "pause" and t0 <= inc.start <= t1:
            x1, x2 = x(inc.start), x(min(inc.end, t1))
            parts.append(f'<rect x="{x1:.1f}" y="{pad_t}" width="{max(x2-x1,1):.1f}" height="{h}" fill="#ffffff" fill-opacity=".05"><title>Paused</title></rect>')
    for key, label, col in SERIES:
        pts = []
        for i in range(0, len(rows), step):
            chunk = rows[i:i + step]
            vals = []
            for r in chunk:
                v = ((r.get("disks") or {}).get(letter) or {}).get("busy") if key == "disk_busy" else r.get(key)
                if isinstance(v, (int, float)):
                    vals.append(v)
            if vals:
                pts.append(f"{x(chunk[0]['_dt']):.1f},{y(max(vals)):.1f}")
        if pts:
            parts.append(f'<polyline fill="none" stroke="{col}" stroke-width="1.4" stroke-linejoin="round" points="{" ".join(pts)}"/>')
    parts.append("</svg>")
    legend = '<div class="legend">' + "".join(f'<span><i style="background:{c}"></i>{E(l)}</span>' for _, l, c in SERIES) + \
             '<span><i style="background:#ff5d5d"></i>OBS skip</span><span><i style="background:#ff9b5d"></i>Missing/repeated frames in file</span><span><i style="background:#c58bff"></i>Windows event</span></div>'
    return f'<div class="chart">{legend}{"".join(parts)}</div>'


def _metrics_cell(m):
    if not m:
        return '<span class="muted">no monitor data</span>'
    bits = []
    for k, lab, unit in (("gpu_3d", "GPU", "%"), ("gpu_enc", "Encoder", "%"), ("nv_enc", "NVENC", "%"), ("cpu", "CPU", "%"),
                         ("disk_busy", "Drive", "% busy"), ("disk_write_ms", "Write", " ms"), ("ram", "RAM", "%"), ("nv_temp", "GPU temp", "°C"),
                         ("obs_render_ms", "OBS render", " ms")):
        v = m.get(k)
        if isinstance(v, (int, float)):
            bits.append(f"{lab} {v:.0f}{unit}")
    if m.get("top_gpu"):
        bits.append("GPU users: " + ", ".join(f"{n} {v:.0f}%" for n, v in m["top_gpu"][:2]))
    if m.get("top_cpu"):
        bits.append("Busiest: " + ", ".join(f"{n} {v:.0f}%" for n, v in m["top_cpu"][:3]))
    return E(" · ".join(bits)) if bits else '<span class="muted">no monitor data</span>'


def _row(k, a, b=None, ok=None):
    if b is None:
        return f"<tr><td>{E(k)}</td><td>{E(str(a))}</td></tr>"
    if str(b) in ("—", "unknown", "None"):
        return f'<tr><td>{E(k)}</td><td>{E(str(a))}</td><td>{E(str(b))}</td><td class="muted">—</td></tr>'
    if ok is None:
        ok = str(a).lower().replace(" ", "") == str(b).lower().replace(" ", "")
    return f'<tr><td>{E(k)}</td><td>{E(str(a))}</td><td>{E(str(b))}</td><td class="{"match" if ok else "mismatch"}">{"Match" if ok else "Mismatch"}</td></tr>'


def build(res: Result) -> str:
    log = res.log
    crit = [f for f in res.findings if f.severity == "critical"]
    warn = [f for f in res.findings if f.severity == "warning"]
    total_render = sum((r.output.render_lagged or 0) for r in res.recordings)
    total_enc = sum((r.output.encode_skipped or 0) for r in res.recordings)
    live_render = sum(i.frames for i in res.incidents if i.kind == "render")
    live_enc = sum(i.frames for i in res.incidents if i.kind == "encode")
    file_missing = sum((r.video.gap_frames_total if r.video and r.video.ok else 0) for r in res.recordings)
    n_inc = len([i for i in res.incidents if i.kind not in ("pause",)])

    if crit:
        vcls, vtxt = "crit", f"<b>Problems found.</b> {len(crit)} serious issue(s) and {len(warn)} warning(s). The most important: {E(crit[0].title)}."
    elif warn:
        vcls, vtxt = "warn", f"<b>Mostly OK, with {len(warn)} thing(s) worth fixing.</b> The top item: {E(warn[0].title)}."
    else:
        vcls, vtxt = "good", "<b>Clean session.</b> No skipped frames or setting conflicts were found."

    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>Recording Report — {res.created:%Y-%m-%d %H:%M}</title><style>{CSS}</style></head><body><div class='wrap'>"]
    out.append(f"<h1>Recording Report</h1><div class='muted'>Created {res.created:%A, %B %d %Y at %I:%M %p}</div>")
    src = []
    if log:
        src.append(f"OBS log: {E(os.path.basename(log.path))} ({E(log.obs_version)})")
    if res.monitor_path:
        src.append(f"Monitor data: {len(res.samples):,} samples")
    vids = [r for r in res.recordings if r.video and r.video.ok]
    if vids:
        src.append(f"{len(vids)} recording file(s) checked" + (" (deep scan)" if any(r.video.deep_scanned for r in vids) else ""))
    out.append(f"<div class='muted small'>{' · '.join(src)}</div>")
    out.append(f"<div class='verdict {vcls}'>{vtxt}</div>")
    out.append("<div class='cards'>")
    for k, v in (("Rendering lag (OBS)", f"{max(total_render, live_render):,}"), ("Encoding skips (OBS)", f"{max(total_enc, live_enc):,}"),
                 ("Missing frames in file", f"{file_missing:,}" if vids else "—"), ("Timeline incidents", f"{n_inc:,}"),
                 ("Recordings", f"{len(res.recordings)}")):
        out.append(f"<div class='card'><div class='k'>{E(k)}</div><div class='v'>{E(v)}</div></div>")
    out.append("</div>")
    for n in res.notes:
        out.append(f"<div class='note'>{E(n)}</div>")

    # findings
    out.append("<h2>Findings</h2>")
    if not res.findings:
        out.append("<div class='note'>Nothing to report.</div>")
    for f in res.findings:
        body = []
        if f.detail:
            body.append(f"<div>{E(f.detail)}</div>")
        if getattr(f, "loc", None):
            body.append("<div class='where'><div class='wh'>Where it comes from on your PC</div><dl>"
                        + "".join(f"<dt>{E(k)}</dt><dd>{E(v)}</dd>" for k, v in f.loc) + "</dl></div>")
        if f.why:
            body.append(f"<div style='margin-top:6px'><b>Why it matters:</b> {E(f.why)}</div>")
        if f.fix:
            body.append(f"<div style='margin-top:6px'><b>What to try:</b> {E(f.fix)}</div>")
        openattr = " open" if f.severity in ("critical", "warning") else ""
        out.append(f"<details class='f'{openattr}><summary><span class='badge b-{f.severity}'>{E(f.severity)}</span>"
                   f"<span class='t'>{E(f.title)}</span><span class='rec'>{E(f.recording)}</span></summary>"
                   f"<div class='body'>{''.join(body) or '<span class=muted>—</span>'}</div></details>")

    # per recording
    for r in res.recordings:
        o = r.output
        out.append(f"<h2>{E(r.label)}</h2>")
        kv = []
        if o.path:
            kv += ["File", E(o.path)]
        kv += ["Started", f"{o.start:%I:%M:%S %p}"]
        if o.stop:
            kv += ["Stopped", f"{o.stop:%I:%M:%S %p}", "Length (without pauses)", fmt_t(o.duration_seconds)]
        if o.scene:
            kv += ["Scene at start", E(o.scene)]
        if r.drive.get("letter"):
            d = r.drive
            kv += ["Recording drive", E(" ".join(x for x in (d.get("letter"), d.get("name"), f"({d.get('bus')} {d.get('media')})" if d.get("bus") else "", d.get("type") if d.get("type") not in ("unknown", "fixed") else "") if x))]
        if o.encode_total:
            kv += ["Encoding skips", f"{o.encode_skipped:,} / {o.encode_total:,} ({100*o.encode_skipped/o.encode_total:.2f}%)"]
        if o.render_lagged is not None:
            kv += ["Rendering lag", f"{o.render_lagged:,} ({o.render_lagged_pct}%)"]
        out.append("<div class='kv'>" + "".join(f"<div>{kv[i]}</div><div>{kv[i+1]}</div>" for i in range(0, len(kv), 2)) + "</div>")

        if r.stats:
            st = r.stats
            out.append("<h3>What the PC was doing during this recording</h3>")
            out.append(_svg_chart(r, r.drive.get("letter", ""), [i for i in res.incidents if i.recording == r.label]))
            rows = []
            for k, lab, unit in (("gpu_3d", "GPU 3D load", "%"), ("gpu_enc", "GPU video encoder", "%"), ("cpu", "CPU", "%"), ("ram", "RAM", "%"),
                                 ("disk_busy", f"Recording drive busy", "%"), ("disk_write_ms", "Drive write latency", " ms"),
                                 ("nv_temp", "GPU temperature", " °C"), ("obs_render_ms", "OBS frame render time", " ms")):
                s = st.get(k)
                if s:
                    rows.append(f"<tr><td>{E(lab)}</td><td class='num'>{s['avg']}{unit}</td><td class='num'>{s['p95']}{unit}</td><td class='num'>{s['max']}{unit}</td></tr>")
            if rows:
                out.append("<table><tr><th>Measure</th><th>Average</th><th>95% of the time below</th><th>Peak</th></tr>" + "".join(rows) + "</table>")
            if st.get("samples") and not st.get("obs_connected"):
                out.append("<div class='note'>OBS live stats were not available (WebSocket not connected), so skip times come from the file only.</div>")

        # settings vs file
        vs, v, enc = o.video_settings, r.video, o.encoder or {}
        if vs or (v and v.ok):
            out.append("<h3>Settings check — OBS vs. the actual file</h3><table><tr><th>Setting</th><th>OBS</th><th>File</th><th></th></tr>")
            vv = (v.video if v and v.ok else {}) or {}
            if vs:
                eres = f"{enc.get('width')}x{enc.get('height')}" if enc.get("width") else vs.output_res
                out.append(_row("Resolution", eres, f"{vv.get('width')}x{vv.get('height')}" if vv else "—"))
                out.append(_row("Frame rate", f"{vs.fps_value:g}", f"{v.nominal_fps:.3f}".rstrip('0').rstrip('.') if vv else "—"))
                rng = {"pc": "Full", "jpeg": "Full", "tv": "Limited", "mpeg": "Limited"}.get(vv.get("color_range", ""), vv.get("color_range", "—")) if vv else "—"
                out.append(_row("Color range", vs.color_range or "—", rng))
                cs_map = {"bt709": "Rec. 709", "bt470bg": "Rec. 601", "smpte170m": "Rec. 601", "bt2020nc": "Rec. 2100", "bt2020c": "Rec. 2100"}
                out.append(_row("Color space", vs.color_space or "—", cs_map.get(vv.get("color_space", ""), vv.get("color_space", "—")) if vv else "—"))
                bitf = "10-bit" if vs.format.upper() in ("P010", "I010") else "8-bit"
                fbit = ("10-bit" if "10" in (vv.get("pix_fmt") or "") else "8-bit") if vv else "—"
                out.append(_row("Bit depth (picture)", f"{bitf} ({vs.format})", f"{fbit} ({vv.get('pix_fmt')})" if vv else "—", ok=(bitf == fbit) if vv else None))
            if enc:
                codec = str(enc.get("codec", "")).upper()
                fcodec = (vv.get("codec") or "—").upper() if vv else "—"
                okc = (codec == fcodec or (codec == "HEVC" and fcodec == "HEVC") or (codec in ("H264", "H.264", "AVC") and fcodec == "H264")) if codec and vv else None
                out.append(_row("Codec", f"{codec or '—'} ({enc.get('encoder','')})", fcodec, ok=okc))
                prof = str(enc.get("profile", "—"))
                fprof = str(vv.get("profile", "—")) if vv else "—"
                out.append(_row("Profile", prof, fprof, ok=prof.lower().replace(" ", "") in fprof.lower().replace(" ", "") if vv else None))
                extra = ", ".join(f"{k} {enc[k]}" for k in ("rate_control", "cqp", "bitrate", "preset", "tuning", "multipass", "lookahead", "b-frames", "keyint") if k in enc)
                out.append(f"<tr><td>Encoder settings</td><td colspan='3'>{E(extra)}</td></tr>")
            if v and v.ok:
                out.append(f"<tr><td>File</td><td colspan='3'>{E(v.container)} · {v.duration/60:.1f} min · {v.size_bytes/1073741824:.1f} GiB · "
                           f"{v.bitrate/1e6:.0f} Mb/s · {v.frame_count:,} frames (expected {v.expected_frames:,})"
                           + (f" · audio: " + ", ".join(f"{a['codec']} {a['sample_rate']} Hz" for a in v.audio) if v.audio else "") + "</td></tr>")
            out.append("</table>")
            if o.devices:
                out.append("<h3>Capture devices at recording start</h3><table><tr><th>Source</th><th>Device</th><th>Resolution</th><th>FPS</th><th>Format</th><th>Audio</th><th>Buffering</th></tr>")
                for d in o.devices:
                    cf = vs.fps_value if vs else 0
                    fcls = "mismatch" if cf and d.fps and abs(d.fps - cf) > 0.001 else ""
                    acls = "mismatch" if log and d.sample_rate and log.audio_rate and d.sample_rate != log.audio_rate else ""
                    out.append(f"<tr><td>{E(d.name)}</td><td>{E(d.device)}</td><td>{E(d.resolution)}</td><td class='{fcls}'>{d.fps:g}</td>"
                               f"<td>{E(d.format)}</td><td class='{acls}'>{d.sample_rate or '—'} Hz{' (OBS: ' + str(log.audio_rate) + ')' if acls else ''}</td><td>{E(d.buffering)}</td></tr>")
                out.append("</table>")

        # incidents for this recording
        incs = [i for i in res.incidents if i.recording == r.label]
        out.append(f"<h3>Timeline of incidents ({len([i for i in incs if i.kind != 'pause'])})</h3>")
        if not incs:
            out.append("<div class='note'>No incidents in this recording.</div>")
        else:
            out.append("<table><tr><th>Clock time</th><th>In video</th><th>What happened</th><th>PC at that moment</th><th>Possible reasons</th></tr>")
            for i in incs[:600]:
                causes = "".join(f"<li><span class='badge b-{c}'>{E(c)}</span> {E(t)}</li>" for t, c in i.causes)
                out.append(f"<tr><td class='num'>{i.start:%I:%M:%S %p}</td><td class='num'>{fmt_t(i.video_t)}</td><td>{E(i.title)}</td>"
                           f"<td class='small'>{_metrics_cell(i.metrics) if i.kind not in ('pause','log','winevent','audio-gap','freeze') else ''}</td>"
                           f"<td class='small'><ul class='c'>{causes}</ul></td></tr>")
            if len(incs) > 600:
                out.append(f"<tr><td colspan='5' class='muted'>…and {len(incs)-600} more</td></tr>")
            out.append("</table>")

    # Windows events
    rel = [e for e in res.winevents if e["meaning"]]
    if res.winevents:
        out.append(f"<h2>Windows event log ({len(rel)} relevant of {len(res.winevents)})</h2>")
        if rel:
            out.append("<table><tr><th>Time</th><th>Source</th><th>Meaning</th><th>Message</th></tr>")
            for e in rel[:200]:
                out.append(f"<tr><td class='num'>{e['ts']:%I:%M:%S %p}</td><td>{E(e['provider'])} #{e['id']}</td><td>{E(e['meaning'])}</td><td class='small muted'>{E(e['message'][:220])}</td></tr>")
            out.append("</table>")
        else:
            out.append("<div class='note'>No hardware, driver, disk or crash events during the recording window.</div>")

    # system
    if log:
        out.append("<h2>System</h2><div class='kv'>")
        s = log.system
        for k, lab in (("cpu", "CPU"), ("memory", "Memory"), ("windows", "Windows"), ("obs_gpu", "OBS renders on"), ("admin", "Run as admin"), ("game_mode", "Game Mode")):
            if s.get(k):
                out.append(f"<div>{E(lab)}</div><div>{E(s[k])}</div>")
        for a in log.adapters:
            out.append(f"<div>GPU</div><div>{E(a['name'])} · driver {E(a.get('driver',''))} · HAGS {E((a.get('hags') or '').split(' (')[0])}</div>")
        for d in log.displays:
            out.append(f"<div>Display</div><div>{E(d.get('name',''))} {E(d.get('size',''))} @ {E(d.get('refresh',''))} Hz · {E(d.get('space',''))}</div>")
        out.append(f"<div>OBS audio</div><div>{log.audio_rate} Hz</div></div>")

    out.append("""<h2>How to read this report</h2><div class='small muted'>
<p><b>Rendering lag</b> — OBS couldn't draw the scene in time (usually the GPU is busy). <b>Encoding lag</b> — the encoder couldn't keep up, so frames were skipped before reaching the file.
<b>Missing frames in file</b> — gaps in the recording's frame timestamps. <b>Repeated frames</b> (deep scan) — the same picture shown twice in a row during motion, which is how most skips look in the finished video.</p>
<p>Confidence badges: <span class='badge b-high'>high</span> strong evidence at that exact moment · <span class='badge b-medium'>medium</span> plausible contributor · <span class='badge b-low'>low</span> weak/indirect evidence.
Possible reasons are educated guesses from what was measured — not proof.</p></div>""")
    out.append(f"<footer>Live Recording Analyzer · report generated {res.created:%Y-%m-%d %H:%M:%S}</footer></div></body></html>")
    return "".join(out)


def save(res: Result, folder: str) -> str:
    os.makedirs(folder, exist_ok=True)
    name = f"OBS report {res.created:%Y-%m-%d %H-%M-%S}.html"
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(build(res))
    return path
