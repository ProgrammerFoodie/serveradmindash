import { api } from "../api.js";
import { badge, dataTable, el, fmtAgo, fmtDateTime, fmtDuration, fmtNum, kv, panel } from "../util.js";

const when = (ts) => [fmtDateTime(ts), el("div", { class: "sub" }, fmtAgo(ts))];

export default {
  id: "security",
  title: "Security",

  create() {
    const tg = panel("Alerts and Telegram");
    const events = dataTable({
      sortKey: "ts", sortDir: -1, empty: "No alerts in the last 7 days",
      columns: [
        { key: "ts", label: "When", render: (r) => [fmtDateTime(r.ts), el("div", { class: "sub" }, fmtAgo(r.ts))] },
        { key: "kind", label: "Event", render: (r) => (r.kind === "alert_resolved" ? badge("resolved", "ok") : badge(r.kind === "event" ? "notice" : r.severity, r.severity === "crit" ? "crit" : r.severity === "warn" ? "warn" : "info")) },
        { key: "message", label: "What", cls: "wrap" }],
    });
    const result = el("span", { class: "result" });
    const testButton = el("button", { class: "btn small", type: "button" }, "Send test alert");
    testButton.addEventListener("click", async () => {
      testButton.disabled = true; result.className = "result"; result.textContent = "sending…";
      try {
        const res = await api.post("/api/telegram/test");
        result.className = `result ${res.ok ? "ok" : "bad"}`;
        result.textContent = res.ok ? "Sent: check Telegram." : `Failed: ${res.error}`;
      } catch (e) { result.className = "result bad"; result.textContent = `Failed: ${e.message}`; }
      testButton.disabled = false;
      lastAlertFetch = 0;
    });
    let lastAlertFetch = 0;
    async function refreshAlerts() {
      if (Date.now() - lastAlertFetch < 15000) return;
      lastAlertFetch = Date.now();
      try {
        const d = await api.get("/api/alerts");
        const t = d.telegram;
        tg.set(
          el("div", { class: "rowline" },
            el("span", null, t.configured ? badge("Telegram on", "ok") : badge("Telegram not set up", "warn"),
              t.configured ? el("span", { class: "sub" }, `  ${fmtNum(t.sent)} sent${t.last_ok ? `, last ${fmtAgo(t.last_ok)}` : ""}`) : null),
            el("span", null, result, " ", testButton)),
          t.last_error ? el("p", { class: "err" }, `Last Telegram error: ${t.last_error}`) : null,
          t.configured ? null : el("p", { class: "sub" }, "To get alerts on your phone, put telegram.bot_token and telegram.chat_id into config.json on the server and restart the dashboard. Alerts always show in the banner on every tab."),
          events.el);
        events.setRows(d.events.map((e) => ({ ...e })));
      } catch (e) { tg.set(el("p", { class: "err" }, `Unavailable: ${e.message}`)); }
    }
    tg.set(el("p", { class: "empty" }, "Loading…"));
    const ips = dataTable({ sortKey: "attempts", sortDir: -1, empty: "None", columns: [{ key: "ip", label: "Address", cls: "mono" }, { key: "attempts", label: "Attempts", num: true }] });
    const users = dataTable({ sortKey: "attempts", sortDir: -1, empty: "None", columns: [{ key: "user", label: "Username tried", cls: "mono wrap" }, { key: "attempts", label: "Attempts", num: true }] });
    const failed = dataTable({
      sortKey: "time", sortDir: -1, empty: "No failed attempts in the last 24 hours",
      columns: [
        { key: "time", label: "When", render: (r) => when(r.time) },
        { key: "user", label: "Username", cls: "mono wrap" }, { key: "ip", label: "From", cls: "mono" },
        { key: "kind", label: "Reason", render: (r) => (r.kind === "invalid_user" ? "no such user" : "wrong credentials") }],
    });
    const accepted = dataTable({
      sortKey: "time", sortDir: -1, empty: "No successful logins in the last 24 hours",
      columns: [
        { key: "time", label: "When", render: (r) => when(r.time) },
        { key: "user", label: "User" }, { key: "ip", label: "From", cls: "mono" }, { key: "method", label: "Method" }],
    });
    const sessions = dataTable({
      sortKey: "since", sortDir: -1, empty: "Nobody is signed in",
      columns: [
        { key: "user", label: "User" }, { key: "from", label: "From", cls: "mono", render: (r) => r.from || "local" },
        { key: "service", label: "Via" }, { key: "since", label: "Since", num: true, render: (r) => `${fmtAgo(r.since)}` }],
    });
    const history = dataTable({
      sortKey: "time", sortDir: -1, empty: "No login history",
      columns: [
        { key: "type", label: "Event", render: (r) => (r.type === "reboot" ? badge("reboot", "info") : "login") },
        { key: "user", label: "User" }, { key: "from", label: "From", cls: "mono" }, { key: "tty", label: "Terminal" },
        { key: "time", label: "When", render: (r) => when(r.time) },
        { key: "end", label: "Duration", num: true, render: (r) => (r.end ? fmtDuration(r.end - r.time) : "–") }],
    });
    const packages = dataTable({
      sortKey: "name", sortDir: 1, empty: "Everything is up to date",
      columns: [
        { key: "name", label: "Package", render: (r) => [el("b", null, r.name), r.security ? [" ", badge("security", "warn")] : null] },
        { key: "from", label: "Installed", cls: "mono wrap" }, { key: "version", label: "Available", cls: "mono wrap" }, { key: "suite", label: "From" }],
    });

    const upd = panel("Updates and reboot"), ssh = panel("SSH logins, last 24 hours"), f2b = panel("fail2ban (automatic bans)");
    const now = panel("Signed in now"), hist = panel("Login history"), recentFail = panel("Recent failed attempts"), recentOk = panel("Recent successful SSH logins");
    const pkgList = el("details", null, el("summary", { class: "muted" }, "Show packages"), packages.el);
    const ipsPanel = panel("Most active addresses (failed)"), usersPanel = panel("Most tried usernames");
    ipsPanel.set(ips.el); usersPanel.set(users.el); now.set(sessions.el); hist.set(history.el); recentFail.set(failed.el); recentOk.set(accepted.el);
    const wait = (p, s) => { if (!s) p.set(el("p", { class: "empty" }, "Waiting for data…")); else if (s.data.error) p.set(el("p", { class: "err" }, `Unavailable: ${s.data.error}`)); else return true; return false; };

    return {
      el: el("div", { class: "rows" },
        tg.el,
        el("div", { class: "grid" }, upd.el, ssh.el, f2b.el, now.el),
        el("div", { class: "grid wide" }, ipsPanel.el, usersPanel.el, recentFail.el, recentOk.el, hist.el)),
      update(live) {
        const s = live.sections;
        refreshAlerts();
        if (wait(upd, s.updates)) {
          const d = s.updates.data, un = d.last_unattended || {};
          upd.set(
            el("div", { class: "big" }, fmtNum(d.count), el("small", null, "updates waiting"), d.security_count ? [" ", badge(`${d.security_count} security`, "warn")] : null),
            d.reboot_required ? el("p", null, badge("reboot required", "warn"), " since ", fmtAgo(d.reboot_required_since), d.reboot_packages.length ? el("div", { class: "sub" }, `because of: ${d.reboot_packages.join(", ")}`) : null) : el("p", { class: "muted" }, "No reboot needed."),
            kv([["Package lists refreshed", d.last_apt_update ? fmtAgo(d.last_apt_update) : "unknown"],
              ["Automatic updates last ran", un.time ? `${fmtAgo(un.time)}` : un.error ? "log not readable" : "unknown"],
              ["…and said", un.message]]),
            d.count ? pkgList : null);
          packages.setRows(d.packages);
        }
        if (wait(ssh, s.ssh_auth)) {
          const d = s.ssh_auth.data;
          ssh.set(kv([["Successful logins", badge(fmtNum(d.accepted), "ok")], ["Failed password or key", fmtNum(d.failed)], ["Unknown usernames tried", fmtNum(d.invalid_user)]]),
            el("div", { class: "sub" }, "Internet-facing SSH is probed all day long; what matters is whether any attempt succeeds."));
          ips.setRows(d.top_ips); users.setRows(d.top_users); failed.setRows(d.recent_failed); accepted.setRows(d.recent_accepted);
        }
        if (wait(f2b, s.fail2ban)) {
          const d = s.fail2ban.data;
          f2b.set(d.jails.length ? d.jails.map((j) => el("div", { class: "rows" },
            el("div", { class: "rowline" }, el("b", null, j.name), badge(`${j.currently_banned} banned now`, j.currently_banned ? "warn" : "ok")),
            kv([["Total bans", fmtNum(j.total_banned)], ["Failures being counted", fmtNum(j.currently_failed)], ["Failures ever", fmtNum(j.total_failed)]]),
            j.banned_ips.length ? el("div", { class: "chips" }, j.banned_ips.map((ip) => badge(ip, "warn"))) : null)) : el("p", { class: "empty" }, "No jails configured."));
        }
        const l = s.logins;
        if (wait(now, l)) {
          now.set(sessions.el);
          sessions.setRows(l.data.sessions || []);
          history.setRows(l.data.history || []);
          if (l.data.sessions_error) now.set(el("p", { class: "err" }, `Unavailable: ${l.data.sessions_error}`));
          if (l.data.history_error) hist.set(el("p", { class: "err" }, `Unavailable: ${l.data.history_error}`));
        }
      },
    };
  },
};
