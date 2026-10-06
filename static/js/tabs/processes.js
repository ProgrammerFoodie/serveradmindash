import { badge, dataTable, el, fmtAgo, fmtBytes, fmtNum, fmtPct, fmtRate, isNum, kv, panel } from "../util.js";

const STATES = { R: "running", S: "sleeping", D: "waiting for disk", Z: "zombie", T: "stopped", I: "idle", t: "traced", X: "dead" };
const stateBadge = (s) => badge(STATES[s] || s, s === "D" ? "warn" : s === "Z" ? "crit" : "");

/** Details of one process with the Terminate, then Force kill, buttons. The server re-checks everything. */
function openProcess(ctx, row) {
  const unit = row.unit || "";
  const service = unit.endsWith(".service") ? unit.slice(0, -".service".length) : "";
  const guarded = Boolean(service) && ctx.session.protected.includes(service);
  const outcome = el("p", { class: "sub" });
  const term = el("button", { class: "btn", type: "button", disabled: guarded }, "Terminate (SIGTERM)");
  const kill = el("button", { class: "btn danger", type: "button", hidden: true }, "Force kill (SIGKILL)");

  async function send(signal) {
    const forced = signal === "KILL";
    const ok = await ctx.confirm({
      title: forced ? `Force kill ${row.name}?` : `Terminate ${row.name}?`,
      lines: [`PID ${row.pid}, user ${row.user}${unit ? `, part of ${unit}` : ""}`, row.cmd.length > 160 ? `${row.cmd.slice(0, 160)}…` : row.cmd],
      warning: forced ? ["The process is stopped immediately and cannot save anything or clean up."]
        : ["It is asked to shut down and may take a moment. A program can ignore this request."],
      confirmLabel: forced ? "Force kill" : "Terminate", danger: true });
    if (!ok) return;
    const result = await ctx.act("/api/action/process", { pid: row.pid, signal, start_ticks: row.start_ticks, name: row.name });
    if (!result) return;                               // the toast already says why it was refused
    if (result.exited) { ctx.closeDialog(); return; }
    outcome.textContent = "It is still running. You can force it to stop.";
    kill.hidden = false;
  }
  term.addEventListener("click", () => send("TERM"));
  kill.addEventListener("click", () => send("KILL"));

  ctx.openDialog(`${row.name} (PID ${row.pid})`, el("div", { class: "dlg-body" },
    kv([["User", row.user], ["Belongs to", unit || "–"], ["CPU", isNum(row.cpu_pct) ? fmtPct(row.cpu_pct) : "–"], ["RAM", fmtBytes(row.rss)],
      ["Started", fmtAgo(row.start_time)], ["State", STATES[row.state] || row.state]]),
    el("p", { class: "mono" }, row.cmd),
    guarded ? el("p", { class: "err" }, `Protected: this belongs to ${service}, which keeps you connected. Restart the service from the Services tab instead.`) : null,
    outcome, el("div", { class: "confirm-actions" }, term, kill)));
}

export default {
  id: "processes",
  title: "Processes",

  create(ctx) {
    let last = null;
    const summary = el("div", { class: "chips" });
    const search = el("input", { type: "search", placeholder: "Filter by name, user, PID or command", "aria-label": "Filter processes", autocomplete: "off" });
    const count = el("span", { class: "muted" });
    const table = dataTable({
      sortKey: "cpu_pct", sortDir: -1, empty: "No process matches",
      onRowClick: ctx.session.actions ? (row) => openProcess(ctx, row) : undefined,
      columns: [
        { key: "pid", label: "PID", num: true },
        { key: "name", label: "Process", render: (r) => el("b", null, r.name) },
        { key: "user", label: "User" },
        { key: "cpu_pct", label: "CPU", num: true, render: (r) => (isNum(r.cpu_pct) ? fmtPct(r.cpu_pct) : "…") },
        { key: "rss", label: "RAM", num: true, render: (r) => fmtBytes(r.rss) },
        { key: "swap", label: "Swap", num: true, render: (r) => (r.swap ? fmtBytes(r.swap) : "–") },
        { key: "threads", label: "Threads", num: true },
        { key: "state", label: "State", render: (r) => stateBadge(r.state) },
        { key: "io", label: "Disk read / write", num: true, value: (r) => (r.read_Bps || 0) + (r.write_Bps || 0),
          render: (r) => (isNum(r.read_Bps) ? `${fmtRate(r.read_Bps)} / ${fmtRate(r.write_Bps)}` : "–") },
        { key: "start_time", label: "Started", num: true, render: (r) => fmtAgo(r.start_time) },
        { key: "cmd", label: "Command", cls: "cmd", sortable: false, render: (r) => el("div", { class: "clamp", title: r.cmd }, r.cmd) },
      ],
    });
    const services = dataTable({
      sortKey: "mem", sortDir: -1,
      columns: [
        { key: "name", label: "Service", render: (r) => el("b", null, r.name) },
        { key: "cpu_pct", label: "CPU", num: true, render: (r) => (isNum(r.cpu_pct) ? fmtPct(r.cpu_pct) : "…") },
        { key: "memory", label: "RAM", num: true, render: (r) => fmtBytes(r.memory) },
        { key: "swap", label: "Swap", num: true, render: (r) => fmtBytes(r.swap) },
        { key: "mem", label: "RAM + swap", num: true, value: (r) => (r.memory || 0) + (r.swap || 0), render: (r) => fmtBytes((r.memory || 0) + (r.swap || 0)) },
        { key: "pids", label: "Tasks", num: true },
      ],
    });
    const procPanel = panel("All processes", count);
    const svcPanel = panel("Resources by service");
    svcPanel.set(services.el);
    const note = el("p", { class: "empty" }, "Collecting the first sample…");
    procPanel.set(el("div", { class: "toolbar" }, search, ctx.session.actions ? el("span", { class: "sub" }, "Click a process for details, Terminate and Force kill") : null), note, summary, table.el);

    function paintTable() {
      if (!last) return;
      const q = search.value.trim().toLowerCase();
      const rows = q ? last.processes.filter((p) => `${p.pid} ${p.name} ${p.user} ${p.cmd}`.toLowerCase().includes(q)) : last.processes;
      count.textContent = `${rows.length} of ${last.processes.length}`;
      table.setRows(rows);
    }
    search.addEventListener("input", paintTable);

    return {
      el: el("div", { class: "rows" }, procPanel.el, svcPanel.el),
      update(live) {
        const p = live.sections.processes;
        note.hidden = !!(p && !p.data.error);
        if (!p) return;                                   // the first sample is still being taken
        if (p.data.error) { note.className = "err"; note.textContent = `Unavailable: ${p.data.error}`; return; }
        last = p.data;
        summary.replaceChildren(...[
          badge(`${fmtNum(last.count)} processes`), badge(`${fmtNum(last.threads_total)} threads`), badge(`${fmtNum(last.kernel_threads)} kernel threads`),
          ...Object.entries(last.states).sort().map(([s, n]) => badge(`${n} ${STATES[s] || s}`, s === "Z" ? "crit" : s === "D" ? "warn" : "")),
          last.processes.some((r) => isNum(r.cpu_pct)) ? null : badge("measuring CPU…", "info")].filter(Boolean));
        paintTable();
        const groups = live.sections.cgroups;
        if (groups && groups.data && !groups.data.error) services.setRows(groups.data.services);
      },
    };
  },
};
