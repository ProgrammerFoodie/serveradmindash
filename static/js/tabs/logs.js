import { LineChart, seriesFromRows } from "../chart.js";
import { badge, dataTable, el, fmtAgo, fmtBytes, fmtDateTime, fmtNum, level, panel } from "../util.js";

const PRIORITY = { 0: "emergency", 1: "alert", 2: "critical", 3: "error", 4: "warning" };
const statusLevel = (st) => (st >= 500 ? "crit" : st >= 400 ? "warn" : "");

export default {
  id: "logs",
  title: "Logs & web",

  create(ctx) {
    const certs = dataTable({
      sortKey: "days_left", sortDir: 1, empty: "No certificates found",
      columns: [
        { key: "name", label: "Certificate", render: (r) => el("b", null, r.name) },
        { key: "domains", label: "Names", cls: "mono wrap", sortable: false, render: (r) => (r.domains || []).join(", ") },
        { key: "issuer", label: "Issuer" },
        { key: "days_left", label: "Expires in", num: true, render: (r) => badge(`${fmtNum(r.days_left, 0)} days`, level(ctx.thresholds.ssl_days, r.days_left, true)) },
        { key: "expires", label: "Date", num: true, render: (r) => fmtDateTime(r.expires) }],
    });
    const paths = dataTable({ sortKey: "requests", sortDir: -1, empty: "No requests", columns: [{ key: "path", label: "Path", cls: "mono wrap" }, { key: "requests", label: "Requests", num: true }] });
    const clients = dataTable({ sortKey: "requests", sortDir: -1, empty: "No requests", columns: [{ key: "ip", label: "Client address", cls: "mono" }, { key: "requests", label: "Requests", num: true }] });
    const agents = dataTable({ sortKey: "requests", sortDir: -1, empty: "No requests", columns: [{ key: "agent", label: "Browser / program", cls: "wrap" }, { key: "requests", label: "Requests", num: true }] });
    const errors5 = dataTable({
      sortKey: "time", sortDir: -1, empty: "No server errors",
      columns: [
        { key: "time", label: "When", render: (r) => [fmtDateTime(r.time), el("div", { class: "sub" }, fmtAgo(r.time))] },
        { key: "status", label: "Status", render: (r) => badge(String(r.status), "crit") },
        { key: "ip", label: "Client", cls: "mono" }, { key: "request", label: "Request", cls: "mono wrap" }],
    });
    const units = dataTable({
      sortKey: "errors", sortDir: -1, empty: "No warnings or errors in the last 24 hours",
      columns: [
        { key: "unit", label: "Source", render: (r) => el("b", null, r.unit) },
        { key: "errors", label: "Errors", num: true, render: (r) => (r.errors ? badge(String(r.errors), "crit") : "0") },
        { key: "warnings", label: "Warnings", num: true },
        { key: "last", label: "Latest", num: true, render: (r) => fmtAgo(r.last) },
        { key: "last_message", label: "Latest message", cls: "mono wrap", sortable: false }],
    });
    const recent = dataTable({
      sortKey: "time", sortDir: -1, empty: "Nothing to show",
      columns: [
        { key: "time", label: "When", render: (r) => [fmtDateTime(r.time), el("div", { class: "sub" }, fmtAgo(r.time))] },
        { key: "priority", label: "Level", render: (r) => badge(PRIORITY[r.priority] || String(r.priority), r.priority <= 3 ? "crit" : "warn") },
        { key: "unit", label: "Source" }, { key: "message", label: "Message", cls: "mono wrap", sortable: false }],
    });
    const fwSources = dataTable({ sortKey: "count", sortDir: -1, empty: "None", columns: [{ key: "ip", label: "Blocked address", cls: "mono" }, { key: "count", label: "Blocks", num: true }] });
    const fwPorts = dataTable({ sortKey: "count", sortDir: -1, empty: "None", columns: [{ key: "port", label: "Port tried", cls: "mono" }, { key: "count", label: "Blocks", num: true }] });

    const reqChart = new LineChart({ title: "Web requests per hour (last 24 h)", unit: "count", bars: true, showMax: false,
      series: [{ metric: "requests", label: "requests", color: 1 }, { metric: "errors", label: "4xx and 5xx", color: 4 }],
      note: "A 503 can be a deliberate maintenance page, so server errors here are not always failures." });
    const minChart = new LineChart({ title: "Web requests per minute (last hour)", unit: "count", bars: true, showMax: false, series: [{ metric: "requests", label: "requests", color: 1 }] });

    const sslP = panel("SSL certificates"), webP = panel("Web server (nginx), last 24 hours"), errP = panel("Recent server errors (5xx)"), errLog = panel("nginx error log");
    const jP = panel("System journal: warnings and errors, last 24 hours"), fwP = panel("Firewall blocks, last 24 hours");
    const jTable = panel("Recent messages"), jUnits = panel("By source");
    const wait = (p, s) => { if (!s) p.set(el("p", { class: "empty" }, "Waiting for data…")); else if (s.data.error) p.set(el("p", { class: "err" }, `Unavailable: ${s.data.error}`)); else return true; return false; };
    errP.set(errors5.el); jTable.set(recent.el); jUnits.set(units.el);

    const pathsP = panel("Most requested paths"), clientsP = panel("Busiest clients"), agentsP = panel("Browsers and programs");
    pathsP.set(paths.el); clientsP.set(clients.el); agentsP.set(agents.el);
    const fwA = panel("Most blocked addresses"), fwB = panel("Most probed ports");
    fwA.set(fwSources.el); fwB.set(fwPorts.el);

    return {
      el: el("div", { class: "rows" },
        sslP.el, webP.el,
        el("div", { class: "grid wide" }, reqChart.el, minChart.el),
        el("div", { class: "grid wide" }, pathsP.el, clientsP.el, agentsP.el),
        errP.el, errLog.el, jP.el,
        el("div", { class: "grid wide" }, jUnits.el, jTable.el),
        el("div", { class: "grid wide" }, fwP.el, fwA.el, fwB.el)),
      update(live) {
        const s = live.sections;
        if (wait(sslP, s.ssl)) { sslP.set(certs.el); certs.setRows(s.ssl.data.certificates); }

        if (wait(webP, s.nginx)) {
          const d = s.nginx.data;
          webP.set(el("div", { class: "chips" },
            badge(`${fmtNum(d.requests)} requests`), badge(`${fmtBytes(d.bytes)} sent`),
            ...Object.entries(d.status).map(([k, n]) => badge(`${fmtNum(n)} × ${k}`, k === "5xx" ? "crit" : k === "4xx" ? "warn" : ""))));
          const now = Math.floor(Date.now() / 1000);
          reqChart.setData({ step_s: 3600, range_s: 24 * 3600, end: now, events: [], series: {
            requests: d.per_hour.map((h) => [h.hour, h.requests, h.requests]),
            errors: d.per_hour.map((h) => [h.hour, h.errors, h.errors]) } });
          const minute = Math.floor(now / 60) * 60;
          minChart.setData(seriesFromRows(d.per_minute.map((v, i) => ({ t: minute - (60 - i) * 60, v })), "requests", 60, 3600, now));
          paths.setRows(d.top_paths); clients.setRows(d.top_ips); agents.setRows(d.top_agents);
          errors5.setRows(d.recent_5xx);
          errLog.set(d.error_log.length ? el("pre", { class: "mono scroll-y" }, d.error_log.join("\n")) : el("p", { class: "empty" }, "The error log is empty."));
        } else { errLog.set(el("p", { class: "empty" }, "–")); }

        if (wait(jP, s.journal)) {
          const d = s.journal.data;
          jP.set(el("div", { class: "chips" }, badge(`${fmtNum(d.errors)} errors`, d.errors ? "crit" : "ok"), badge(`${fmtNum(d.warnings)} warnings`, d.warnings ? "warn" : "ok")),
            el("div", { class: "sub" }, "Firewall blocks are counted separately below."));
          units.setRows(d.units); recent.setRows(d.recent);
          const fw = d.firewall || { blocks: 0, top_sources: [], top_ports: [] };
          fwP.set(el("div", { class: "big" }, fmtNum(fw.blocks), el("small", null, "connection attempts blocked")),
            el("div", { class: "sub" }, "The firewall dropping these is the intended behaviour; this just shows what is knocking."));
          fwSources.setRows(fw.top_sources); fwPorts.setRows(fw.top_ports);
        }
      },
    };
  },
};
