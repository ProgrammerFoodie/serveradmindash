import { api } from "../api.js";
import { powerCard } from "../power.js";
import { openLogs } from "../logview.js";
import { badge, dataTable, el, fmtAgo, fmtBytes, fmtDateTime, fmtDuration, fmtPct, fmtUntil, isNum, panel } from "../util.js";

// What a restart costs, for the services you could cut yourself off with.
const RESTART_WARNING = {
  ssh: ["Open SSH sessions stay connected; new logins work again as soon as it is back."],
  tailscaled: ["This is the VPN that carries this page: you will lose the connection for a few seconds."],
  nginx: ["Every website on this server is interrupted for a moment."],
  "server-dashboard": ["This page reloads by itself when the dashboard is back."],
  "admin-dns": ["Your devices cannot find this dashboard by name for a moment (answers they already cached keep working)."],
};

async function serviceAction(ctx, kind, name, op, isProtected) {
  const ask = {
    start: { lines: [`Start ${name}.`], label: "Start" },
    restart: { lines: [`Restart ${name}: it stops and starts again.`], warning: isProtected ? RESTART_WARNING[name] || [] : [], label: "Restart" },
    stop: { lines: [`Stop ${name}. It stays stopped until someone starts it again (or the server reboots).`, "Alerts about it are muted until it is running again."], label: "Stop", danger: true },
  }[op];
  if (!(await ctx.confirm({ title: `${ask.label} ${name}?`, lines: ask.lines, warning: ask.warning || [], confirmLabel: ask.label, danger: !!ask.danger }))) return;
  await ctx.act("/api/action/service", { kind, name, op });
}

/** Buttons that fit a service's current state. Stop is shown disabled for protected services, so it is clear why. */
function actionButtons(ctx, kind, name, running, isProtected) {
  if (!ctx.session.actions) return [];
  const button = (op, text, cls, disabled, title) => el("button", { class: `btn small ${cls}`, type: "button", disabled, title,
    onclick: () => serviceAction(ctx, kind, name, op, isProtected) }, text);
  return running
    ? [button("restart", "Restart", "", false, `Restart ${name}`),
       button("stop", "Stop", "danger", isProtected, isProtected ? "Protected: stopping it would cut you off. Restart it instead." : `Stop ${name}`)]
    : [button("start", "Start", "primary", false, `Start ${name}`)];
}

function statusBadge(u) {
  if (!u.exists) return badge("not installed");
  const label = u.sub && u.sub !== u.active ? `${u.active} (${u.sub})` : u.active;
  if (u.active === "active") return badge(label, "ok");
  if (u.active === "failed" || (u.result && u.result !== "success")) return badge(label, "crit");
  return badge(label, u.enabled === "enabled" ? "warn" : "");        // stopped: worth a look only if it should be running
}

export default {
  id: "services",
  title: "Services",

  create(ctx) {
    const logButton = (kind, name) => el("button", { class: "btn small", type: "button", onclick: () => openLogs(ctx, kind, name) }, "Logs");
    const units = dataTable({
      sortKey: "name", sortDir: 1,
      rowClass: (r) => (r.exists ? "" : "dim"),
      columns: [
        { key: "name", label: "Service", render: (r) => [el("b", null, r.name), r.protected ? [" ", badge("protected")] : null,
          el("div", { class: "sub" }, r.description !== r.unit ? r.description : "")] },
        { key: "active", label: "Status", render: statusBadge },
        { key: "enabled", label: "Starts at boot", render: (r) => (r.exists ? r.enabled || "static" : "–") },
        { key: "uptime_s", label: "Running for", num: true, render: (r) => fmtDuration(r.uptime_s) },
        { key: "cpu_pct", label: "CPU", num: true, render: (r) => (isNum(r.cpu_pct) ? fmtPct(r.cpu_pct) : "–") },
        { key: "mem", label: "RAM", num: true, value: (r) => r.mem, render: (r) => fmtBytes(r.mem) },
        { key: "swap", label: "Swap", num: true, render: (r) => (r.swap ? fmtBytes(r.swap) : "–") },
        { key: "restarts", label: "Restarts", num: true },
        { key: "pid", label: "PID", num: true },
        { key: "logs", label: "", sortable: false, render: (r) => (r.exists
          ? el("div", { class: "actions" }, logButton("systemd", r.name), actionButtons(ctx, "systemd", r.name, ["active", "activating", "reloading"].includes(r.active), r.protected)) : null) },
      ],
    });
    const supervisor = dataTable({
      sortKey: "name", sortDir: 1, empty: "No supervisor programs",
      columns: [
        { key: "name", label: "Program", render: (r) => el("b", null, r.name) },
        { key: "state", label: "Status", render: (r) => [badge(r.state, r.state === "RUNNING" ? "ok" : r.state === "STOPPED" ? "warn" : "crit"), r.detail ? el("div", { class: "sub" }, r.detail) : null] },
        { key: "uptime_s", label: "Running for", num: true, render: (r) => fmtDuration(r.uptime_s) },
        { key: "rss", label: "RAM", num: true, render: (r) => fmtBytes(r.rss) },
        { key: "pid", label: "PID", num: true },
        { key: "logs", label: "", sortable: false, render: (r) => el("div", { class: "actions" }, logButton("supervisor", r.name),
          actionButtons(ctx, "supervisor", r.name, r.state === "RUNNING", false)) },
      ],
    });
    const timers = dataTable({
      sortKey: "next", sortDir: 1, empty: "No timers",
      columns: [
        { key: "unit", label: "Timer", render: (r) => r.unit.replace(/\.timer$/, "") },
        { key: "activates", label: "Runs", render: (r) => (r.activates || "").replace(/\.service$/, "") },
        { key: "next", label: "Next run", num: true, render: (r) => (r.next ? `${fmtUntil(r.next)} · ${fmtDateTime(r.next)}` : "–") },
        { key: "last", label: "Last run", num: true, render: (r) => (r.last ? fmtAgo(r.last) : "never") },
      ],
    });

    const audit = dataTable({
      sortKey: "ts", sortDir: -1, empty: "Nothing has been done from the dashboard yet",
      columns: [
        { key: "ts", label: "When", render: (r) => [fmtDateTime(r.ts), el("div", { class: "sub" }, fmtAgo(r.ts))] },
        { key: "action", label: "Action", render: (r) => [el("b", null, r.action), " ", badge(r.ok ? "done" : "refused", r.ok ? "ok" : "warn")] },
        { key: "target", label: "Target", cls: "mono wrap" },
        { key: "detail", label: "Result", cls: "wrap" },
        { key: "ip", label: "From", cls: "mono" }],
    });
    let lastAudit = 0, seenSeq = 0;
    async function refreshAudit() {
      if (ctx.actionSeq !== seenSeq) { seenSeq = ctx.actionSeq; lastAudit = 0; }
      if (Date.now() - lastAudit < 15000) return;
      lastAudit = Date.now();
      try { audit.setRows((await api.get("/api/audit?limit=50")).entries); } catch (e) { console.warn("audit refresh failed:", e.message); }
    }
    const sys = panel("System services (systemd)"), sup = panel("Supervisor programs"), failed = panel("Failed units"), tm = panel("Scheduled tasks (timers)");
    const auditPanel = panel("Recent actions from this dashboard");
    auditPanel.set(audit.el);
    sys.set(units.el); sup.set(supervisor.el); tm.set(timers.el);
    const ok = (s) => s && s.data && !s.data.error;
    const note = (p, s, empty) => { if (!s) p.set(el("p", { class: "empty" }, "Waiting for data…")); else if (s.data.error) p.set(el("p", { class: "err" }, `Unavailable: ${s.data.error}`)); else return true; return false; };

    const power = ctx.session.admin && ctx.session.admin.power ? powerCard(ctx) : null;     // only where config.json switches it on

    return {
      el: el("div", { class: "rows" }, power ? power.el : null, failed.el, sys.el, sup.el, tm.el, auditPanel.el),
      update(live) {
        const s = live.sections;
        if (ctx.session.actions) refreshAudit(); else auditPanel.el.hidden = true;
        const groups = new Map(ok(s.cgroups) ? s.cgroups.data.services.map((g) => [g.name, g]) : []);
        if (note(sys, s.systemd)) {
          sys.set(units.el);
          units.setRows(s.systemd.data.watched.map((u) => {
            const g = groups.get(u.name) || {};
            return { ...u, cpu_pct: g.cpu_pct, mem: g.memory ?? u.memory, swap: g.swap };
          }));
          timers.setRows(s.systemd.data.timers);
          const f = s.systemd.data.failed;
          failed.set(f.length ? f.map((x) => el("div", { class: "rowline" }, el("span", null, badge("failed", "crit"), " ", el("b", null, x.unit)), el("span", { class: "muted" }, x.description)))
            : el("p", { class: "empty" }, "No failed units."));
        }
        if (note(sup, s.supervisor)) { sup.set(supervisor.el); supervisor.setRows(s.supervisor.data.programs); }
      },
    };
  },
};
