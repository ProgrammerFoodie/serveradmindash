// A small canvas line/bar chart: time on x, one or more series, tooltip on hover or touch.
// Written for this dashboard so no third-party code has to be shipped or trusted.

import { el, fmtBytes, fmtNum, fmtRate, isNum } from "./util.js";

const UNITS = {
  percent: (v) => `${fmtNum(v, v > 0 && v < 10 ? 1 : 0)}%`,
  bytes: (v) => fmtBytes(v),
  rate: (v) => fmtRate(v),
  ms: (v) => `${fmtNum(v, v < 10 ? 1 : 0)} ms`,
  number: (v) => fmtNum(v, Math.abs(v) < 10 ? 2 : Math.abs(v) < 100 ? 1 : 0),
  count: (v) => fmtNum(v, 0),
};
const TOOLTIP_DIGITS = { percent: (v) => `${fmtNum(v, 1)}%` };

/** Round up to a "nice" axis maximum: 1, 2, 2.5, 5 or 10 times a power of ten. */
export function niceMax(v) {
  if (!(v > 0)) return 1;
  const pow = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (v <= m * pow) return m * pow;
  return 10 * pow;
}

/** Axis maximum for byte values: round in KB/MB/GB steps that split cleanly into four (1, 2, 4, 8, 10, 20, ...). */
export function niceBytesMax(v) {
  if (!(v > 0)) return 1024;
  let unit = 1;
  while (v / unit >= 1024) unit *= 1024;
  const scaled = v / unit;
  const steps = [1, 2, 4, 8, 10, 20, 40, 80, 100, 200, 400, 800, 1024];
  return steps.find((c) => scaled <= c) * unit;
}

/** Index of the entry in a sorted [[t, ...], ...] array whose t is closest to `t`. */
export function nearest(points, t) {
  let lo = 0, hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid][0] < t) lo = mid + 1; else hi = mid;
  }
  if (lo > 0 && Math.abs(points[lo - 1][0] - t) <= Math.abs(points[lo][0] - t)) lo--;
  return lo;
}

function axisTime(ts, rangeS) {
  const d = new Date(ts * 1000);
  return rangeS <= 36 * 3600
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { month: "short", day: "numeric" });
}

export class LineChart {
  /**
   * series: [{metric, label, color 1-6}]; unit: percent|bytes|rate|ms|number|count;
   * yMax: fixed axis maximum (else automatic, at least minMax); bars: draw columns instead of a line;
   * bare: no card around it, for placing inside another card.
   */
  constructor({ title, unit = "number", series, yMax = null, minMax = 0, fill = false, bars = false, showMax = true, note = "", bare = false }) {
    Object.assign(this, { title, unit, series, yMax, minMax, fill, bars, showMax });
    this.fmt = UNITS[unit] || UNITS.number;
    this.res = null;
    this.hover = null;
    this.canvas = el("canvas", { role: "img", "aria-label": title });
    this.tip = el("div", { class: "tip", hidden: true });
    this.plot = el("div", { class: "chart" }, this.canvas, this.tip);
    this.noteEl = el("p", { class: "chart-note", hidden: !note }, note);
    this.el = el("section", { class: bare ? "chart-block" : "card" },
      el("div", { class: "chart-head" },
        el("h3", null, title),
        el("div", { class: "legend" }, series.map((s, i) => el("span", { class: `c${s.color || i + 1}` }, s.label)))),
      this.plot, this.noteEl);

    new ResizeObserver(() => this.draw()).observe(this.plot);
    this.plot.addEventListener("pointermove", (e) => this.onPointer(e));
    this.plot.addEventListener("pointerdown", (e) => this.onPointer(e));
    this.plot.addEventListener("pointerleave", () => this.setHover(null));
    this.plot.addEventListener("pointercancel", () => this.setHover(null));
  }

  /** res: the /api/history response ({series: {metric: [[t, avg, max]]}, step_s, end, range_s, events}). */
  setData(res) { this.res = res; this.setHover(null, false); this.draw(); }

  geometry() {
    return { w: this.plot.clientWidth, h: this.plot.clientHeight, right: 8, top: 8, bottom: 20 };
  }

  yMaxValue() {
    if (this.yMax != null) return this.yMax;
    let m = this.minMax;
    for (const s of this.series) {
      for (const p of this.res.series[s.metric] || []) {
        const v = this.showMax && this.res.step_s >= 60 && !this.bars ? p[2] : p[1];
        if (isNum(v) && v > m) m = v;
      }
    }
    return this.unit === "bytes" || this.unit === "rate" ? niceBytesMax(m * 1.05) : niceMax(m * 1.05);
  }

  draw() {
    const { w, h, right, top, bottom } = this.geometry();
    if (!w || !h) return;                                   // not visible yet; the ResizeObserver redraws when it is
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    const ctx = this.canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const css = getComputedStyle(this.plot);
    const color = (n) => css.getPropertyValue(`--c${n}`).trim() || "#5aa9ff";
    const dim = css.getPropertyValue("--dim").trim() || "#8b98aa";
    const grid = css.getPropertyValue("--line").trim() || "#263140";
    ctx.font = "11px system-ui, sans-serif";

    const res = this.res;
    const hasData = res && this.series.some((s) => (res.series[s.metric] || []).length);
    if (!hasData) {
      ctx.fillStyle = dim; ctx.textAlign = "center";
      ctx.fillText(res ? "No data for this range yet" : "Loading…", w / 2, h / 2);
      return;
    }

    const t1 = res.end, t0 = res.end - res.range_s, ymax = this.yMaxValue();
    const labels = [0, 1, 2, 3, 4].map((i) => this.fmt((ymax * i) / 4));
    ctx.textAlign = "right";
    const left = Math.min(w / 3, Math.ceil(Math.max(...labels.map((l) => ctx.measureText(l).width))) + 12);   // room for the widest label
    const px = (t) => left + ((t - t0) / (t1 - t0)) * (w - left - right);
    const py = (v) => top + (1 - Math.min(v, ymax) / ymax) * (h - top - bottom);
    this.scale = { px, py, t0, t1, left, right, w };

    // grid and y labels
    ctx.textAlign = "right"; ctx.textBaseline = "middle"; ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = Math.round(py((ymax * i) / 4)) + 0.5;
      ctx.strokeStyle = grid; ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(w - right, y); ctx.stroke();
      ctx.fillStyle = dim; ctx.fillText(labels[i], left - 6, y);
    }
    // x labels
    ctx.textBaseline = "alphabetic";
    const ticks = w < 420 ? 3 : 5;
    for (let i = 0; i < ticks; i++) {
      const t = t0 + ((t1 - t0) * (i + 0.5)) / ticks;
      ctx.textAlign = "center"; ctx.fillStyle = dim; ctx.fillText(axisTime(t, res.range_s), px(t), h - 5);
    }

    const gap = res.step_s * 3.5;
    this.series.forEach((s, i) => {
      const pts = (res.series[s.metric] || []).filter((p) => isNum(p[1]));
      if (!pts.length) return;
      const c = color(s.color || i + 1);
      if (this.bars) {
        ctx.fillStyle = c;
        const bw = Math.max(1, (px(t0 + res.step_s) - px(t0)) - 2);   // a bucket starts at its timestamp
        for (const p of pts) { const y = py(p[1]); ctx.fillRect(px(p[0]) + 1, y, bw, h - bottom - y); }
        return;
      }
      if (this.showMax && res.step_s >= 60) {              // faint line for the peak inside each averaged point
        ctx.strokeStyle = c; ctx.globalAlpha = 0.28; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
        this.trace(ctx, pts, 2, px, py, gap); ctx.stroke(); ctx.setLineDash([]); ctx.globalAlpha = 1;
      }
      ctx.strokeStyle = c; ctx.lineWidth = 1.6; ctx.lineJoin = "round";
      this.trace(ctx, pts, 1, px, py, gap); ctx.stroke();
      if (this.fill || this.series.length === 1) {
        ctx.save(); ctx.globalAlpha = 0.12; ctx.fillStyle = c;
        let start = null, last = null;
        const close = () => { if (start !== null) { ctx.lineTo(px(last), h - bottom); ctx.lineTo(px(start), h - bottom); ctx.closePath(); ctx.fill(); } };
        ctx.beginPath();
        pts.forEach((p, j) => {
          if (j === 0 || p[0] - pts[j - 1][0] > gap) { close(); ctx.beginPath(); ctx.moveTo(px(p[0]), py(p[1])); start = p[0]; }
          else ctx.lineTo(px(p[0]), py(p[1]));
          last = p[0];
        });
        close(); ctx.restore();
      }
    });

    // markers for alerts and actions
    for (const ev of res.events || []) {
      if (!/^(alert|action|service)/.test(ev.kind) || ev.ts < t0 || ev.ts > t1) continue;
      ctx.strokeStyle = ev.severity === "crit" ? color(4) : ev.severity === "warn" ? color(3) : dim;
      ctx.globalAlpha = 0.6; ctx.setLineDash([2, 3]);
      ctx.beginPath(); ctx.moveTo(px(ev.ts), top); ctx.lineTo(px(ev.ts), h - bottom); ctx.stroke();
      ctx.setLineDash([]); ctx.globalAlpha = 1;
    }

    if (this.hover) {                                      // crosshair and dots
      const x = px(this.hover.t);
      ctx.strokeStyle = dim; ctx.globalAlpha = 0.6; ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, h - bottom); ctx.stroke(); ctx.globalAlpha = 1;
      this.series.forEach((s, i) => {
        const p = this.hover.points[i];
        if (!p || !isNum(p[1])) return;
        ctx.fillStyle = color(s.color || i + 1); ctx.beginPath(); ctx.arc(x, py(p[1]), 3.5, 0, Math.PI * 2); ctx.fill();
      });
    }
  }

  /** Add one polyline per run of points; a gap larger than `gap` seconds starts a new run. */
  trace(ctx, pts, col, px, py, gap) {
    ctx.beginPath();
    pts.forEach((p, j) => {
      const v = isNum(p[col]) ? p[col] : p[1];
      if (j === 0 || p[0] - pts[j - 1][0] > gap) ctx.moveTo(px(p[0]), py(v)); else ctx.lineTo(px(p[0]), py(v));
    });
  }

  onPointer(e) {
    if (!this.res || !this.scale) return;
    const rect = this.plot.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const { t0, t1, left, right, w } = this.scale;
    const t = t0 + ((x - left) / (w - left - right)) * (t1 - t0);
    // use the series with the most points as the time grid
    let base = null;
    for (const s of this.series) {
      const pts = this.res.series[s.metric] || [];
      if (pts.length && (!base || pts.length > base.length)) base = pts;
    }
    if (!base) return;
    const at = base[nearest(base, t)][0];
    if (Math.abs(at - t) > Math.max(this.res.step_s * 2, (t1 - t0) / 40)) return this.setHover(null);
    const points = this.series.map((s) => {
      const pts = this.res.series[s.metric] || [];
      if (!pts.length) return null;
      const p = pts[nearest(pts, at)];
      return Math.abs(p[0] - at) <= this.res.step_s ? p : null;
    });
    this.setHover({ t: at, points }, true, e.clientX - rect.left);
  }

  setHover(hover, redraw = true, x = 0) {
    this.hover = hover;
    if (!hover) { this.tip.hidden = true; if (redraw) this.draw(); return; }
    const showMax = this.showMax && this.res.step_s >= 60 && !this.bars;
    const fmt = TOOLTIP_DIGITS[this.unit] || this.fmt;
    const rows = this.series.map((s, i) => {
      const p = hover.points[i];
      if (!p || !isNum(p[1])) return null;
      const peak = showMax && isNum(p[2]) && p[2] > p[1] * 1.05 + 0.01 ? el("i", null, `  peak ${fmt(p[2])}`) : null;
      return el("div", null, `${this.series.length > 1 ? s.label + ": " : ""}${fmt(p[1])}`, peak);
    });
    const stamp = new Date(hover.t * 1000).toLocaleString([], this.res.step_s < 60
      ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }
      : { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    this.tip.replaceChildren(el("b", null, stamp), ...rows.filter(Boolean));   // a null would be printed as the word "null"
    this.tip.hidden = false;
    const tipW = this.tip.offsetWidth, plotW = this.plot.clientWidth;
    this.tip.style.left = `${Math.max(0, Math.min(plotW - tipW, x + 12 + tipW > plotW ? x - tipW - 12 : x + 12))}px`;
    this.tip.style.top = "4px";
    if (redraw) this.draw();
  }
}

/** Turn an array like [{hour, requests}] into the shape LineChart.setData expects. */
export function seriesFromRows(rows, key, stepS, rangeS, end) {
  return { step_s: stepS, range_s: rangeS, end, events: [], series: { [key]: rows.map((r) => [r.t, r.v, r.v]) } };
}

const MAX_METRICS_PER_REQUEST = 40;

/** Loads one history request per refresh for a set of charts and hands each its data. */
export class ChartGroup {
  constructor(ctx) { this.ctx = ctx; this.charts = []; this.last = 0; this.rangeUsed = null; this.busy = false; }

  add(chart) {
    if (!this.charts.includes(chart)) { this.charts.push(chart); this.last = 0; }   // new charts load at the next refresh
    return chart;
  }

  /** Stop loading data for a chart (its card was closed); it keeps what it has drawn. */
  remove(chart) { this.charts = this.charts.filter((c) => c !== chart); }

  metrics() { return [...new Set(this.charts.flatMap((c) => c.series.map((s) => s.metric)))]; }

  /** Refresh if the range changed, charts were added, or the data is older than 30 s. */
  async refresh(force = false) {
    const stale = Date.now() - this.last > 30000;
    if (this.busy || !this.charts.length || (!force && !stale && this.rangeUsed === this.ctx.range)) return;
    this.busy = true;
    const range = this.ctx.range;
    try {
      const names = this.metrics();
      const parts = [];
      for (let i = 0; i < names.length; i += MAX_METRICS_PER_REQUEST) parts.push(this.ctx.history(names.slice(i, i + MAX_METRICS_PER_REQUEST), range));
      const results = await Promise.all(parts);
      const merged = { ...results[0], series: Object.assign({}, ...results.map((r) => r.series)) };
      this.charts.forEach((c) => c.setData(merged));
      this.last = Date.now();
      this.rangeUsed = range;
    } catch (e) {
      if (e.name !== "AuthError") console.warn("history refresh failed:", e.message);
    } finally { this.busy = false; }
  }
}
