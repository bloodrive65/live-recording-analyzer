/* Live Recording Analyzer — online report viewer.
   The report travels inside the link after "#r=". It is decompressed here, in your browser; nothing is sent to a server. */
(function () {
  const $ = (id) => document.getElementById(id);
  const show = (id) => { ["rp-loading", "rp-empty", "rp-error", "rp"].forEach((k) => ($(k).hidden = k !== id)); };

  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (v == null) continue;
      if (k === "class") n.className = v; else if (k === "text") n.textContent = v; else n.setAttribute(k, v);
    }
    for (const c of kids.flat()) if (c != null) n.append(c.nodeType ? c : document.createTextNode(String(c)));
    return n;
  }
  const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString());

  async function decode(token) {
    const b64 = token.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((token.length + 3) % 4);
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
    return JSON.parse(await new Response(stream).text());
  }

  function chart(c) {
    const W = 1100, H = 240, L = 36, R = 8, T = 8, B = 26, w = W - L - R, h = H - T - B;
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "PC load over the recording, with problem moments marked");
    const add = (tag, a, txt) => { const n = document.createElementNS(NS, tag); for (const k in a) n.setAttribute(k, a[k]); if (txt) n.textContent = txt; svg.append(n); return n; };
    const x = (s) => L + (w * s) / Math.max(c.span, 1), y = (v) => T + h * (1 - Math.min(Math.max(v, 0), 100) / 100);
    for (const g of [0, 25, 50, 75, 100]) {
      add("line", { x1: L, x2: W - R, y1: y(g), y2: y(g), stroke: "#2c2c33" });
      add("text", { x: L - 6, y: y(g) + 4, fill: "#9aa3b2", "font-size": 11, "text-anchor": "end" }, String(g));
    }
    const t0 = new Date(c.t0);
    for (let k = 0; k <= 6; k++) {
      const s = (c.span * k) / 6, d = new Date(t0.getTime() + s * 1000);
      add("text", { x: x(s), y: H - 7, fill: "#9aa3b2", "font-size": 11, "text-anchor": k === 0 ? "start" : k === 6 ? "end" : "middle" },
        d.toLocaleTimeString([], c.span < 900 ? { hour: "numeric", minute: "2-digit", second: "2-digit" } : { hour: "numeric", minute: "2-digit" }));
    }
    const mcol = { render: "#ff5d5d", encode: "#ff5d5d", "file-gap": "#ff9b5d", repeat: "#ff9b5d", winevent: "#c58bff" };
    for (const [s, kind, title] of c.m || []) {
      const ln = add("line", { x1: x(s), x2: x(s), y1: T, y2: T + h, stroke: mcol[kind] || "#9aa3b2", "stroke-opacity": ".55", "stroke-width": 1.5 });
      const tt = document.createElementNS(NS, "title"); tt.textContent = title; ln.append(tt);
    }
    const cols = { gpu: "#ff6b6b", enc: "#f5b748", cpu: "#5fa8ff", disk: "#3ecf8e" };
    for (const [key, vals] of Object.entries(c.s || {})) {
      const pts = [];
      vals.forEach((v, i) => { if (v != null) pts.push(`${x(c.x[i]).toFixed(1)},${y(v).toFixed(1)}`); });
      if (pts.length) add("polyline", { fill: "none", stroke: cols[key], "stroke-width": 1.5, "stroke-linejoin": "round", points: pts.join(" ") });
    }
    const names = { gpu: "GPU 3D %", enc: "Video encoder %", cpu: "CPU %", disk: "Recording drive busy %" };
    const legend = el("div", { class: "rp-legend" },
      Object.keys(c.s || {}).map((k) => el("span", null, el("i", { style: `background:${cols[k]}` }), names[k])),
      el("span", null, el("i", { style: "background:#ff5d5d" }), "Skipped frames"),
      el("span", null, el("i", { style: "background:#ff9b5d" }), "Missing frames in file"));
    return el("div", { class: "rp-chart" }, legend, svg);
  }

  function secs(s) {
    s = +s || 0;
    if (s < 1) return "under 1 s";
    if (s < 60) return Math.round(s) + " s";
    const m = Math.floor(Math.round(s) / 60), r = Math.round(s) % 60;
    return m < 60 ? `${m} min ${String(r).padStart(2, "0")} s` : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
  }

  function table(head, rows) {
    return el("div", { class: "rp-tw" }, el("table", null, el("thead", null, el("tr", null, head.map((h) => el("th", { text: h })))), el("tbody", null, rows)));
  }

  function render(d) {
    const root = $("rp");
    root.textContent = "";
    const created = new Date(d.created);
    document.title = `Recording report — ${created.toLocaleDateString()} · Live Recording Analyzer`;
    root.append(el("h1", { text: "Recording report" }));
    root.append(el("div", { class: "rp-meta" },
      el("span", { text: "Created " + created.toLocaleString([], { dateStyle: "full", timeStyle: "short" }) }),
      (d.sources || []).map((s) => el("span", { text: s })),
      d.app ? el("span", { text: "App " + d.app }) : null));
    const copyBtn = el("button", { class: "btn", type: "button" }, "Copy link");
    copyBtn.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(location.href); copyBtn.textContent = "Link copied"; } catch (e) { prompt("Copy this link:", location.href); }
      setTimeout(() => (copyBtn.textContent = "Copy link"), 2200);
    });
    const printBtn = el("button", { class: "btn", type: "button" }, "Print or save as PDF");
    printBtn.addEventListener("click", () => window.print());
    root.append(el("div", { class: "rp-bar" }, copyBtn, printBtn, el("a", { class: "btn", href: "problems-explained.html" }, "What the problems mean"),
      (d.steps || []).length ? el("a", { class: "btn", href: "#steps" }, "How it was checked") : null));
    root.append(el("p", { class: "rp-priv", text: "Private by design: this report is stored inside the link, not on GitHub. Anyone you send the link to can see it. Folder names are left out." }));

    const [vc, vt, vb] = d.verdict || ["good", "", ""];
    root.append(el("div", { class: "rp-verdict " + vc }, el("b", { text: vt }), el("span", { text: vb })));
    root.append(el("div", { class: "rp-cards" }, (d.cards || []).map(([k, v]) =>
      el("div", { class: "rp-card" }, el("div", { class: "k", text: k }),
        el("div", { class: "v" + (v == null ? "" : v > 0 && k !== "Timeline incidents" ? " bad" : v === 0 ? " ok" : ""), text: fmt(v) })))));
    (d.notes || []).forEach((n) => root.append(el("div", { class: "rp-note", text: n })));
    if (d.compare) root.append(el("div", { class: "rp-cmp" }, el("b", { text: "Compared with the previous recording: " + d.compare.t }),
      el("ul", null, (d.compare.l || []).map((x) => el("li", { text: x })))));

    root.append(el("h2", { text: "Findings" }));
    if (!(d.findings || []).length) root.append(el("div", { class: "rp-note", text: "Nothing to report." }));
    for (const f of d.findings || []) {
      const det = el("details", { class: "rp-f" },
        el("summary", null, el("span", { class: "badge b-" + f.s, text: f.s }), el("span", { class: "t", text: f.t })),
        el("div", { class: "body" },
          f.d ? el("p", { text: f.d }) : null,
          f.l && f.l.length ? el("div", { class: "rp-where" }, el("div", { class: "h", text: "Where it comes from on your PC" }),
            el("dl", null, f.l.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]))) : null,
          f.w ? el("p", null, el("b", { text: "Why it matters: " }), f.w) : null,
          f.f ? el("p", null, el("b", { text: "What to try: " }), f.f) : null));
      if (f.s === "critical" || f.s === "warning") det.open = true;
      root.append(det);
    }

    for (const r of d.recs || []) {
      root.append(el("h2", { text: r.label }));
      root.append(el("div", { class: "rp-kv" }, (r.kv || []).flatMap(([k, v]) => [el("div", { text: k }), el("div", { text: v })])));
      if (r.chart) { root.append(el("h3", { text: "What the PC was doing" })); root.append(chart(r.chart)); }
      if ((r.stats || []).length) root.append(el("div", { style: "margin-top:12px" }, table(["Measure", "Average", "95% of the time below", "Peak"],
        r.stats.map(([l, a, p, m, u]) => el("tr", null, el("td", { text: l }), el("td", { class: "num", text: a + u }), el("td", { class: "num", text: p + u }), el("td", { class: "num", text: m + u }))))));
      if (r.chart === null && !(r.stats || []).length)
        root.append(el("div", { class: "rp-note", text: "No watching data for this recording, so the report can't show what the PC was doing at each problem. Click “Start watching” in the app before your next recording." }));
      if ((r.settings || []).length) {
        root.append(el("h3", { text: "OBS settings vs. the actual file" }));
        root.append(table(["Setting", "OBS", "File", ""], r.settings.map(([k, a, b, ok]) => el("tr", null, el("td", { text: k }), el("td", { text: a }), el("td", { text: b }),
          el("td", { class: ok == null ? "muted" : ok ? "match" : "mismatch", text: ok == null ? "—" : ok ? "Match" : "Mismatch" })))));
      }
      root.append(el("h3", { text: `Timeline of problems (${fmt((r.inc || []).length + (r.more || 0))})` }));
      if (!(r.inc || []).length) root.append(el("div", { class: "rp-note", text: "No problems in this recording." }));
      else {
        const rows = r.inc.map(([t, vt, kind, title, pc, causes]) => el("tr", null, el("td", { class: "num", text: t }), el("td", { class: "num", text: vt || "—" }),
          el("td", { text: title }), el("td", { class: "small", text: pc }),
          el("td", { class: "small" }, el("ul", { class: "c" }, (causes || []).map(([c, conf]) => el("li", null, el("span", { class: "badge b-" + conf, text: conf }), " ", c))))));
        if (r.more) rows.push(el("tr", null, el("td", { colspan: 5, class: "muted", text: `…and ${fmt(r.more)} more (see the copy saved in the app).` })));
        root.append(table(["Clock time", "In video", "What happened", "PC at that moment", "Possible reasons"], rows));
      }
    }
    if ((d.steps || []).length) {
      const total = d.steps.reduce((n, x) => n + (+x[5] || 0), 0);
      root.append(el("h2", { id: "steps", text: "How this recording was checked" }));
      root.append(el("p", { class: "muted small", text: `Every step the analysis took, in the order it ran. Total time: ${secs(total)}.` }));
      const lab = { ok: ["Done", "b-good"], found: ["Found something", "b-warning"], skipped: ["Skipped", "b-low"], failed: ["Couldn't finish", "b-critical"] };
      root.append(el("ol", { class: "rp-steps" }, d.steps.map(([t, title, checks, result, status, sec, rec]) => {
        const [name, cls] = lab[status] || lab.ok;
        return el("li", null,
          el("div", { class: "sh" }, el("b", { text: title }), el("span", { class: "badge " + cls, text: name })),
          el("div", { class: "muted small", text: [t, rec, "took " + secs(sec)].filter(Boolean).join(" · ") }),
          el("div", { class: "small" }, el("span", { class: "muted", text: "Checked: " }), checks || ""),
          el("div", { class: "small" }, el("span", { class: "muted", text: "Result: " }), result || "—"));
      })));
    }
    if ((d.win || []).length) {
      root.append(el("h2", { text: "Windows events during the recording" }));
      root.append(table(["Time", "Source", "Meaning", "Message"], d.win.map(([t, s, m, msg]) => el("tr", null, el("td", { class: "num", text: t }), el("td", { text: s }), el("td", { text: m }), el("td", { class: "small muted", text: msg })))));
    }
    if ((d.sys || []).length) {
      root.append(el("h2", { text: "System" }));
      root.append(el("div", { class: "rp-kv" }, d.sys.flatMap(([k, v]) => [el("div", { text: k }), el("div", { text: v })])));
    }
    root.append(el("h2", { text: "How to read this report" }));
    root.append(el("p", { class: "muted small", text: "Rendering lag: OBS couldn't draw the scene in time (usually the GPU is busy). Encoding skips: the encoder couldn't keep up, so frames never reached the file. Missing frames in file: gaps in the recording's timestamps. Possible reasons are educated guesses from what was measured at that moment, not proof. Confidence: high = strong evidence at that exact moment, medium = plausible, low = weak or indirect." }));
    show("rp");
  }

  async function start() {
    const m = location.hash.match(/[#&]r=([A-Za-z0-9_-]+)/);
    if (!m) { show("rp-empty"); return; }
    try { render(await decode(m[1])); }
    catch (e) { console.error(e); show("rp-error"); }
  }
  // In-page links like #steps must scroll, not replace the #r= report data in the address.
  document.addEventListener("click", (ev) => {
    const a = ev.target.closest && ev.target.closest('a[href^="#"]');
    if (!a || a.getAttribute("href").startsWith("#r=")) return;
    const t = document.getElementById(a.getAttribute("href").slice(1));
    if (!t) return;
    ev.preventDefault();
    t.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  window.addEventListener("hashchange", start);
  start();
})();
