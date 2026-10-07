import { accountBadges, accountDetails, browserLabel, sessionVia } from "../accounts.js";
import { badge, dataTable, el, fmtAgo, fmtDuration, kv, panel, unavailable } from "../util.js";

export default {
  id: "users",
  title: "Users",

  create(ctx) {
    const people = panel("Signed in now"), accountsPanel = panel("Users");
    let data = null, query = "";

    const sessions = dataTable({
      sortKey: "since", sortDir: -1, empty: "Nobody is signed in over SSH or at the console",
      columns: [
        { key: "user", label: "User", render: (r) => el("b", null, r.user) },
        { key: "from", label: "From", cls: "mono" },
        { key: "via", label: "How", value: sessionVia, render: sessionVia },
        { key: "since", label: "Signed in", num: true, render: (r) => fmtAgo(r.since) },
        { key: "idle", label: "Activity", render: (r) => (r.idle ? badge(r.idle_since ? `idle ${fmtAgo(r.idle_since).replace(" ago", "")}` : "idle", "") : badge("active", "ok")) },
        { key: "processes", label: "Processes", num: true },
        { key: "state", label: "State", render: (r) => (r.state === "active" || r.state === "online" ? r.state : badge(r.state, "warn")) },
      ],
    });
    const dashboard = dataTable({
      sortKey: "last_seen", sortDir: -1, empty: "No dashboard sign-ins",
      columns: [
        { key: "ua", label: "Browser", render: (r) => [browserLabel(r.ua), r.current ? [" ", badge("you", "ok")] : null] },
        { key: "ip", label: "From", cls: "mono" },
        { key: "created", label: "Signed in", num: true, render: (r) => fmtAgo(r.created) },
        { key: "last_seen", label: "Last active", num: true, render: (r) => fmtAgo(r.last_seen) },
        { key: "idle_left", label: "Signs out in", num: true, render: (r) => fmtDuration(r.idle_left) },
      ],
    });

    function openAccount(u) {
      const rows = accountDetails(u, data ? data.sessions : []).map(([k, v]) => [k, v === "" ? "–" : v]);
      ctx.openDialog(u.name, el("div", { class: "dlg-body" }, kv(rows)));
    }

    const accounts = dataTable({
      sortKey: "uid", sortDir: 1, onRowClick: openAccount,
      rowClass: (r) => (r.type === "system" ? "dim" : ""),
      columns: [
        { key: "name", label: "Account", render: (r) => [el("b", null, r.name), r.comment && r.comment !== r.name ? el("div", { class: "sub" }, r.comment) : null] },
        { key: "uid", label: "UID", num: true, value: (r) => ({ root: 0, login: 1, system: 2 }[r.type] * 1e6 + r.uid), render: (r) => r.uid },   // people first, then system accounts
        { key: "badges", label: "", sortable: false, render: (r) => el("div", { class: "chips" }, accountBadges(r).map((b) => badge(b.text, b.kind))) },
        { key: "last", label: "Last login", num: true, value: (r) => (r.last_login ? r.last_login.time : null),
          render: (r) => (r.last_login ? [fmtAgo(r.last_login.time), el("div", { class: "sub" }, r.last_login.from || "this machine")] : "never") },
        { key: "shell", label: "Shell", cls: "mono", render: (r) => r.shell.split("/").pop() },
        { key: "processes", label: "Processes", num: true },
      ],
    });

    const search = el("input", { type: "search", placeholder: "Filter by name, full name or group", "aria-label": "Filter accounts", autocomplete: "off" });
    const system = el("input", { type: "checkbox" });
    const note = el("p", { class: "err", hidden: true });
    const count = el("span", { class: "sub" });
    function paint() {
      if (!data) return;
      const q = search.value.trim().toLowerCase();
      const rows = data.users.filter((u) => (system.checked || u.type !== "system")
        && (!q || [u.name, u.comment, ...(u.groups || [])].some((t) => (t || "").toLowerCase().includes(q))));
      accounts.setRows(rows);
      count.textContent = `${rows.length} of ${data.users.length} accounts`;
    }
    search.addEventListener("input", paint);
    system.addEventListener("change", paint);

    people.set(el("h4", { class: "subhead" }, "On this server (SSH and console)"), sessions.el,
      el("h4", { class: "subhead" }, "This dashboard"), dashboard.el);
    accountsPanel.set(el("div", { class: "toolbar" }, search, el("label", { class: "check" }, system, " show system accounts"), count,
      el("span", { class: "sub" }, "Click an account for details")), note, accounts.el);

    return {
      el: el("div", { class: "rows" }, people.el, accountsPanel.el),
      update(live) {
        const s = live.sections.users;
        dashboard.setRows(live.dashboard_sessions || []);
        if (!s) { accountsPanel.set(unavailable()); return; }
        if (s.data.error) { accountsPanel.set(unavailable(null, s.data.error)); return; }
        data = s.data;
        sessions.setRows(data.sessions.map((r) => ({ ...r, via: sessionVia(r) })));
        const notes = Object.values(data.notes || {});
        note.hidden = !notes.length;
        note.textContent = notes.join(" · ");
        paint();
      },
    };
  },
};
