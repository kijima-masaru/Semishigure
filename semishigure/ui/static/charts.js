/* Semishigure UI — chart helpers on top of uPlot (docs/ui-ux-proposal.md §4.4).
   Every chart reads its colours from the CSS tokens so it follows the theme; the
   legend (with live values under the cursor) is uPlot's own HTML legend. */
(function () {
  "use strict";

  const tok = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const mmss = (v) => { const s = Math.max(0, Math.round(v)); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); };
  const fmtN = (u, v) => (v == null ? "–" : String(v));
  const fmtMs = (u, v) => (v == null ? "–" : Number(v).toFixed(2));
  const fmtPct = (u, v) => (v == null ? "–" : Number(v).toFixed(1));
  const reduced = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function axisBase(scale, label, side, fmt) {
    return {
      scale, side, label: label || undefined, labelSize: label ? 14 : 0, labelFont: `600 11px ${tok("--font-sans")}`, font: `11px ${tok("--font-mono")}`,
      stroke: tok("--chart-label"), grid: { stroke: tok("--chart-grid"), width: 1 }, ticks: { stroke: tok("--chart-grid"), width: 1 },
      size: 44, gap: 4, values: fmt ? (u, vals) => vals.map((v) => fmt(u, v)) : undefined,
    };
  }

  function xAxis() {
    return { scale: "x", font: `11px ${tok("--font-mono")}`, stroke: tok("--chart-label"), grid: { stroke: tok("--chart-grid"), width: 1 }, ticks: { stroke: tok("--chart-grid"), width: 1 }, values: (u, vals) => vals.map(mmss), space: 60, size: 28 };
  }

  /** Generic wrapper: keeps the last data so the chart can be rebuilt on a theme change. */
  function Chart(el, buildOpts) {
    this.el = el;
    this.buildOpts = buildOpts;
    this.plot = null;
    this.data = null;
    this.overlay = document.createElement("div");
    this.overlay.className = "overlay";
    this.overlay.textContent = "サンプル待ち";
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(el);
    this._build();
  }
  Chart.prototype._build = function () {
    if (this.plot) { this.plot.destroy(); this.plot = null; }
    this.el.innerHTML = "";
    const width = Math.max(200, this.el.clientWidth || 600);
    const opts = Object.assign({ width, height: 220, cursor: { drag: { x: true, y: false }, sync: { key: "semi" } }, legend: { live: true }, ms: 1, pxAlign: 1 }, this.buildOpts());
    opts.series = opts.series.map((s, i) => (i === 0 ? s : Object.assign({ spanGaps: true, points: { show: false } }, s)));
    this.plot = new uPlot(opts, this.data || opts.series.map(() => []), this.el);
    this.el.appendChild(this.overlay);
    this.overlay.hidden = !!(this.data && this.data[0] && this.data[0].length >= 2);
  };
  Chart.prototype.setData = function (data, msg) {
    this.data = data;
    const ok = data && data[0] && data[0].length >= 2;
    this.overlay.hidden = ok;
    if (!ok) this.overlay.textContent = msg || "サンプル待ち";
    if (this.plot) this.plot.setData(ok ? data : this.plot.series.map(() => []), true);
  };
  Chart.prototype.resize = function () { if (this.plot) { const w = this.el.clientWidth; if (w > 0 && Math.abs(w - this.plot.width) > 2) this.plot.setSize({ width: w, height: this.plot.height }); } };
  Chart.prototype.rebuild = function () { this._build(); };
  Chart.prototype.destroy = function () { this.ro.disconnect(); if (this.plot) this.plot.destroy(); this.plot = null; this.el.innerHTML = ""; };

  /** target / established / established+pending / PBX channels (本, left) and RTP late max (ms, right, 5 ms guide). */
  function runChart(el, withChannels) {
    return new Chart(el, () => ({
      scales: { x: { time: false }, n: { range: (u, min, max) => [0, Math.max(5, Math.ceil((max || 0) * 1.1))] }, ms: { range: (u, min, max) => [0, Math.max(5, Math.ceil((max || 0) * 1.1))] } },
      axes: [xAxis(), axisBase("n", "", 3, fmtN), axisBase("ms", "ms", 1, fmtMs)],
      series: [
        { label: "経過", value: (u, v) => (v == null ? "–" : mmss(v)) },
        { label: "目標", scale: "n", stroke: tok("--chart-0"), width: 2, dash: [6, 4], value: fmtN },
        { label: "確立", scale: "n", stroke: tok("--chart-1"), width: 2.2, value: fmtN },
        { label: "確立 + 接続中", scale: "n", stroke: tok("--chart-2"), width: 1.2, fill: tok("--chart-fill"), value: fmtN },
        { label: "PBX channels", scale: "n", stroke: tok("--chart-3"), width: 1.5, dash: [2, 3], value: fmtN, show: !!withChannels },
        { label: "RTP 遅れ最大 (ms)", scale: "ms", stroke: tok("--chart-4"), width: 1.5, dash: [1.5, 3], value: fmtMs },
      ],
      hooks: { drawAxes: [(u) => { // 5 ms guide line on the right scale
        const y = u.valToPos(5, "ms", true); if (!isFinite(y)) return; const ctx = u.ctx; ctx.save(); ctx.strokeStyle = tok("--chart-4"); ctx.globalAlpha = .5; ctx.setLineDash([2, 4]); ctx.beginPath(); ctx.moveTo(u.bbox.left, y); ctx.lineTo(u.bbox.left + u.bbox.width, y); ctx.stroke(); ctx.restore(); }] },
    }));
  }

  /** Builds the joined data for runChart from load samples (t, target, established, pending, rtp_late_max_ms) and monitor samples (t, channels). */
  function runData(series, monitorSeries) {
    const s = series || [];
    const load = [s.map((p) => p.t), s.map((p) => p.target), s.map((p) => p.established), s.map((p) => p.established + p.pending), s.map((p) => (p.rtp_late_max_ms == null ? null : p.rtp_late_max_ms))];
    const m = (monitorSeries || []).filter((p) => p.channels != null);
    if (!m.length) return [load[0], load[1], load[2], load[3], load[0].map(() => null), load[4]];
    const joined = uPlot.join([load, [m.map((p) => p.t), m.map((p) => p.channels)]]);
    // joined: [x, target, est, est+pend, late, channels] -> reorder to match the series list
    return [joined[0], joined[1], joined[2], joined[3], joined[5], joined[4]];
  }

  /** PBX process %CPU (left) and threads (right). */
  function monitorChart(el, processLabel) {
    return new Chart(el, () => ({
      scales: { x: { time: false }, pct: { range: (u, min, max) => [0, Math.max(10, Math.ceil((max || 0) * 1.1))] }, th: { range: (u, min, max) => [0, Math.max(10, Math.ceil((max || 0) * 1.1))] } },
      axes: [xAxis(), axisBase("pct", "%CPU", 3, fmtPct), axisBase("th", "threads", 1, fmtN)],
      series: [
        { label: "経過", value: (u, v) => (v == null ? "–" : mmss(v)) },
        { label: (processLabel || "PBX") + " %CPU（区間）", scale: "pct", stroke: tok("--chart-2"), width: 2, value: fmtPct },
        { label: "threads", scale: "th", stroke: tok("--chart-3"), width: 1.5, dash: [2, 3], value: fmtN },
      ],
    }));
  }
  function monitorData(monitorSeries) {
    const m = (monitorSeries || []).filter((p) => p.cpu != null || p.nlwp != null);
    return [m.map((p) => p.t), m.map((p) => (p.cpu == null ? null : p.cpu)), m.map((p) => (p.nlwp == null ? null : p.nlwp))];
  }

  /** Established calls of several runs on one time axis (results › 並べて比較). */
  function compareChart(el, labels) {
    const palette = ["--chart-1", "--chart-3", "--chart-2", "--chart-4", "--chart-0"];
    return new Chart(el, () => ({
      scales: { x: { time: false }, n: { range: (u, min, max) => [0, Math.max(5, Math.ceil((max || 0) * 1.1))] } },
      axes: [xAxis(), axisBase("n", "", 3, fmtN)],
      series: [{ label: "経過", value: (u, v) => (v == null ? "–" : mmss(v)) }].concat(labels.map((l, i) => ({ label: l, scale: "n", stroke: tok(palette[i % palette.length]), width: 2, value: fmtN }))),
    }));
  }
  function compareData(runs) {
    const tables = runs.map((r) => { const s = (r.samples || []).filter((p) => p.kind !== "monitor"); const t0 = s.length ? s[0].t : 0; return [s.map((p) => p.t - t0), s.map((p) => p.established)]; });
    if (!tables.length) return [[]];
    return uPlot.join(tables);
  }

  window.SemiCharts = { runChart, runData, monitorChart, monitorData, compareChart, compareData, reduced, mmss };
})();
