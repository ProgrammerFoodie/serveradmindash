import { AgentChart, colorClass } from "../agentchart.js";
import { dataTable, el, fmtAgo, fmtDateTime, fmtDuration, fmtNum, fmtTokens, panel } from "../util.js";

const sum = (a) => a.reduce((x, y) => x + y, 0);

// Time windows, in days ending today; "ytd" counts the days since 1 January of the newest day in the data.
const RANGES = [["1d", "1 day", 1], ["7d", "7 days", 7], ["30d", "30 days", 30], ["365d", "365 days", 365], ["ytd", "Year to date", 0]];

/** The report cut to its last `n` days (all agents that ran in them), with totals recomputed. */
function windowed(d, key) {
  const total = d.days.length;
  let n = RANGES.find((r) => r[0] === key)[2];
  if (!n) { const jan = `${(d.days[total - 1] || "").slice(0, 4)}-01-01`; n = Math.max(1, d.days.filter((x) => x >= jan).length); }
  n = Math.min(n, total);
  const agents = {};
  for (const [name, a] of Object.entries(d.agents)) {
    const w = { tokens: a.tokens.slice(total - n), output: a.output.slice(total - n), seconds: a.seconds.slice(total - n), runs_per_day: a.runs_per_day.slice(total - n) };
    w.runs = sum(w.runs_per_day);
    if (sum(w.tokens) > 0 || sum(w.seconds) > 0 || w.runs > 0) agents[name] = w;
  }
  const all = Object.values(agents);
  return { ...d, days: d.days.slice(total - n), agents,
           totals: { runs: sum(all.map((a) => a.runs)), tokens: sum(all.map((a) => sum(a.tokens))), seconds: sum(all.map((a) => sum(a.seconds))) } };
}

export default {
  id: "agents",
  title: "Agents",

  create() {
    const chart = new AgentChart();
    const buttons = [["tokens", "Tokens"], ["seconds", "Hours"]].map(([mode, label]) =>
      el("button", { type: "button", "aria-pressed": String(mode === "tokens"), onclick: () => {
        chart.setMode(mode); buttons.forEach((b) => b.setAttribute("aria-pressed", String(b === btn(mode))));
      } }, label));
    const btn = (mode) => buttons[mode === "tokens" ? 0 : 1];
    let range = "30d", report = null;
    const rangeButtons = RANGES.map(([key, label]) =>
      el("button", { type: "button", "aria-pressed": String(key === range), onclick: () => {
        range = key; rangeButtons.forEach((b, i) => b.setAttribute("aria-pressed", String(RANGES[i][0] === key)));
        if (report) render();
      } }, label));
    const chartP = panel("Daily usage per sub-agent", el("div", { class: "agent-controls" },
      el("div", { class: "seg", role: "group", "aria-label": "Time window" }, rangeButtons),
      el("div", { class: "seg", role: "group", "aria-label": "Metric" }, buttons)));
    const tableP = panel("By agent type");
    const recentP = panel("Latest runs");

    const table = dataTable({
      sortKey: "tokens", sortDir: -1, empty: "No sub-agent runs in this time window",
      columns: [
        { key: "agent", label: "Agent", render: (r) => el("b", { class: "legend" }, el("span", { class: colorClass(r.index) }, r.agent)) },
        { key: "runs", label: "Runs", num: true },
        { key: "tokens", label: "Tokens", num: true, render: (r) => fmtTokens(r.tokens) },
        { key: "output", label: "Without cache", num: true, render: (r) => fmtTokens(r.output) },
        { key: "seconds", label: "Time", num: true, render: (r) => fmtDuration(r.seconds) },
        { key: "per_day", label: "Per active day", num: true, render: (r) => fmtDuration(r.per_day) }],
    });
    const recent = dataTable({
      sortKey: "start", sortDir: -1, empty: "No runs seen yet",
      columns: [
        { key: "start", label: "When", render: (r) => [fmtDateTime(r.start), el("div", { class: "sub" }, fmtAgo(r.start))] },
        { key: "agent", label: "Agent" },
        { key: "descr", label: "Task", cls: "wrap", sortable: false },
        { key: "tokens", label: "Tokens", num: true, render: (r) => fmtTokens(r.tokens) },
        { key: "seconds", label: "Time", num: true, render: (r) => fmtDuration(r.seconds) }],
    });
    tableP.set(table.el); recentP.set(recent.el);
    let shown = NaN, placeholder = false;           // collection time of the data on screen; whether a placeholder replaced the tables

    return {
      el: el("div", { class: "rows" }, chartP.el, tableP.el, recentP.el),
      update(live) {
        const s = live.sections.agents;
        if (!s) { shown = NaN; report = null; placeholder = true; chartP.set(el("p", { class: "empty" }, "Waiting for data…")); tableP.set(el("p", { class: "empty" }, "–")); recentP.set(el("p", { class: "empty" }, "–")); return; }
        if (s.data.error) { shown = NaN; report = null; placeholder = true; chartP.set(el("p", { class: "err" }, `Unavailable: ${s.data.error}`)); tableP.set(el("p", { class: "empty" }, "–")); recentP.set(el("p", { class: "empty" }, "–")); return; }
        // The collector runs once a minute but the page polls every 5 s: rebuilding the chart for identical data would drop keyboard focus.
        if (s.t !== undefined && shown === s.t) return;
        if (placeholder) { tableP.set(table.el); recentP.set(recent.el); placeholder = false; }
        shown = s.t === undefined ? NaN : s.t;
        report = s.data;
        render();
      },
    };

    function render() {
      const d = windowed(report, range), label = RANGES.find((r) => r[0] === range)[1].toLowerCase();
      const names = chart.setData(d);
      chartP.set(chart.el, el("div", { class: "sub" },
        `${fmtNum(d.totals.runs)} runs · ${fmtTokens(d.totals.tokens)} tokens · ${fmtDuration(d.totals.seconds)} of agent time · days in ${d.timezone || "server time"}`),
        names.length ? null : el("p", { class: "empty" }, `No sub-agent runs in the ${label === "year to date" ? "year to date" : `last ${label}`}.`));
      table.setRows(names.map((n, i) => {
        const a = d.agents[n];
        return { agent: n, index: i, runs: a.runs, tokens: sum(a.tokens), output: sum(a.output), seconds: sum(a.seconds),
                 per_day: sum(a.seconds) / Math.max(1, a.seconds.filter((v) => v > 0).length) };
      }));
      recent.setRows(report.recent);
    }
  },
};
