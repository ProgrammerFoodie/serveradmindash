import { AgentChart, colorClass } from "../agentchart.js";
import { dataTable, el, fmtAgo, fmtDateTime, fmtDuration, fmtNum, fmtTokens, panel } from "../util.js";

const sum = (a) => a.reduce((x, y) => x + y, 0);

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
    const chartP = panel("Daily usage per sub-agent, last 30 days", el("div", { class: "seg", role: "group", "aria-label": "Metric" }, buttons));
    const tableP = panel("By agent type");
    const recentP = panel("Latest runs");

    const table = dataTable({
      sortKey: "tokens", sortDir: -1, empty: "No sub-agent runs in the last 30 days",
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
        if (!s) { shown = NaN; placeholder = true; chartP.set(el("p", { class: "empty" }, "Waiting for data…")); tableP.set(el("p", { class: "empty" }, "–")); recentP.set(el("p", { class: "empty" }, "–")); return; }
        if (s.data.error) { shown = NaN; placeholder = true; chartP.set(el("p", { class: "err" }, `Unavailable: ${s.data.error}`)); tableP.set(el("p", { class: "empty" }, "–")); recentP.set(el("p", { class: "empty" }, "–")); return; }
        // The collector runs once a minute but the page polls every 5 s: rebuilding the chart for identical data would drop keyboard focus.
        if (s.t !== undefined && shown === s.t) return;
        if (placeholder) { tableP.set(table.el); recentP.set(recent.el); placeholder = false; }
        shown = s.t === undefined ? NaN : s.t;
        const d = s.data;
        const names = chart.setData(d);
        chartP.set(chart.el, el("div", { class: "sub" },
          `${fmtNum(d.totals.runs)} runs · ${fmtTokens(d.totals.tokens)} tokens · ${fmtDuration(d.totals.seconds)} of agent time · days in ${d.timezone || "server time"}`),
          names.length ? null : el("p", { class: "empty" }, "No sub-agent runs in the last 30 days."));
        table.setRows(names.map((n, i) => {
          const a = d.agents[n];
          return { agent: n, index: i, runs: a.runs, tokens: sum(a.tokens), output: sum(a.output), seconds: sum(a.seconds),
                   per_day: sum(a.seconds) / Math.max(1, a.seconds.filter((v) => v > 0).length) };
        }));
        recent.setRows(d.recent);
      },
    };
  },
};
