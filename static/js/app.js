// Shell: session, tab routing, the 5-second refresh of the visible tab, the alert statusbar.

import { api, AuthError } from "./api.js";
import { askConfirm } from "./confirm.js";
import { askForm } from "./formdialog.js";
import { watchIdle } from "./idle.js";
import { append, clear, el, fmtAgo, fmtCountdown, fmtDuration, store } from "./util.js";
import overview from "./tabs/overview.js";
import processes from "./tabs/processes.js";
import services from "./tabs/services.js";
import network from "./tabs/network.js";
import security from "./tabs/security.js";
import logs from "./tabs/logs.js";
import users from "./tabs/users.js";

const BASE_TABS = [overview, processes, services, network, security, logs];
let TABS = BASE_TABS;                          // the Users tab is added once the session says config.json switches it on
const RANGES = ["1h", "6h", "24h", "7d", "30d", "90d"];
const REFRESH_MS = 5000;

const view = document.getElementById("view");
const tabsNav = document.getElementById("tabs");
const statusbar = document.getElementById("statusbar");
const powerBarEl = document.getElementById("powerbar");
const dialog = document.getElementById("dlg");
const confirmDialog = document.getElementById("confirm");
const toastEl = document.getElementById("toast");
const statusDot = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");

const rangeButtons = new Set();
const ctx = {
  thresholds: {},
  range: RANGES.includes(store.get("range", "1h")) ? store.get("range", "1h") : "1h",
  history: (metrics, range) => api.history(metrics, range),
  setRange(range) {
    ctx.range = range;
    store.set("range", range);
    rangeButtons.forEach((paint) => paint());
    if (active && active.inst.onRange) active.inst.onRange();
  },
  /** A segmented 1h…90d control; every instance stays in sync with ctx.range. */
  rangePicker() {
    const buttons = RANGES.map((r) => el("button", { type: "button", onclick: () => ctx.setRange(r) }, r));
    const paint = () => buttons.forEach((b) => b.setAttribute("aria-pressed", String(b.textContent === ctx.range)));
    rangeButtons.add(paint); paint();
    return el("div", { class: "seg", role: "group", "aria-label": "History range" }, buttons);
  },
  /** Show a modal with a title and content (used for logs). Returns the content holder. */
  openDialog(title, content, actions = []) {
    clear(dialog);
    append(dialog, [
      el("div", { class: "dlg-head" }, el("h2", { class: "brand" }, title), el("span", { class: "spacer" }), actions,
        el("button", { class: "btn small", type: "button", onclick: () => dialog.close() }, "Close")),
      content]);
    if (!dialog.open) dialog.showModal();
  },
};
dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });   // click on the backdrop

ctx.api = api;
ctx.session = { actions: false, protected: [], admin: {} };
ctx.closeDialog = () => { if (dialog.open) dialog.close(); };
ctx.refreshNow = () => tick();

ctx.toast = (text, kind = "ok", sticky = false) => {
  toastEl.textContent = text;
  toastEl.className = `toast ${kind}`;
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  if (!sticky) toastTimer = setTimeout(() => { toastEl.hidden = true; }, kind === "bad" ? 10000 : 5000);
};

/** Ask before doing something. Resolves true only if the person pressed the confirm button; Cancel has the focus. */
ctx.confirm = (options) => askConfirm(confirmDialog, options);
/** The same for dangerous things: the confirm button stays disabled until `word` has been typed. */
ctx.confirmTyped = ({ word, ...options }) => askConfirm(confirmDialog, { ...options, typed: word });
/** A small form (see formdialog.js). Resolves with the values, or null if cancelled. */
ctx.askForm = (options) => askForm(confirmDialog, options);

/** Perform an action on the server, tell the person what happened, refresh the page data. Returns the result or null. */
ctx.actionSeq = 0;
ctx.busy = false;
ctx.act = async (path, body) => {
  if (ctx.busy) { ctx.toast("Another action is still running. Wait for it to finish.", "warn"); return null; }
  ctx.busy = true;
  document.body.classList.add("busy");                // greys out every action button until this one is done
  ctx.actionSeq++;                                    // lets tabs know their copy of the audit log is stale
  ctx.toast("Working… starting or stopping a service can take up to a minute.", "warn", true);
  try {
    const result = await api.post(path, body);
    ctx.toast(result.detail || "Done", "ok");
    if (result.reconnect) waitForServer({ rebooting: !!result.rebooting });
    else ctx.refreshNow();
    return result;
  } catch (e) {
    if (e instanceof AuthError) ctx.toastHide(); else ctx.toast(e.message, "bad");
    return null;
  } finally {
    ctx.busy = false;
    document.body.classList.remove("busy");
  }
};
ctx.toastHide = () => { toastEl.hidden = true; };

/** After the dashboard restarts itself, or the whole server reboots: wait until it answers again, then reload the page. */
async function waitForServer({ rebooting = false } = {}) {
  ctx.toast(rebooting ? "Rebooting… this page reloads when the server is back." : "Restarting… this page reloads when it is back.", "warn", rebooting);
  const attempts = rebooting ? 240 : 40;                                  // 1.5 s each: a reboot gets up to 6 minutes
  let sawDown = false;
  for (let i = 0; i < attempts; i++) {
    await new Promise((r) => setTimeout(r, 1500));
    try {
      const answering = (await fetch("/healthz", { cache: "no-store" })).ok;
      // A reboot is over only after the server was seen gone and then back; the old server may still answer for a moment.
      if (answering && (rebooting ? sawDown : i > 1)) { location.reload(); return; }
    } catch { sawDown = true; }                                           // still down
    if (rebooting && !sawDown && i > 40) { ctx.toast("The server is still answering. The reboot may not have started.", "warn"); return; }
  }
  ctx.toast(rebooting ? "The server did not come back within six minutes. Check it in the Linode console." : "The dashboard did not come back within a minute. Check the server.", "bad");
}

let toastTimer = null;
let active = null;      // {tab, inst, root}
let timer = null;
let lastOk = 0;
let bannerOpen = null;

// ---- tabs -------------------------------------------------------------------------------------

const instances = new Map();
function buildTabs() {
  tabsNav.replaceChildren(...TABS.map((tab, i) => el("button", {
    class: "tab", role: "tab", id: `tab-${tab.id}`, "aria-selected": "false", "aria-controls": `panel-${tab.id}`,
    onclick: () => { location.hash = tab.id; },
    onkeydown: (e) => {
      const to = e.key === "ArrowRight" ? i + 1 : e.key === "ArrowLeft" ? i - 1 : null;
      if (to !== null) { e.preventDefault(); const n = TABS[(to + TABS.length) % TABS.length]; location.hash = n.id; document.getElementById(`tab-${n.id}`).focus(); }
    },
  }, tab.title)));
}
buildTabs();

function route() {
  const tab = TABS.find((t) => t.id === location.hash.slice(1)) || TABS[0];
  if (active && active.tab === tab) return;
  if (!instances.has(tab.id)) {
    const inst = tab.create(ctx);
    const root = el("div", { id: `panel-${tab.id}`, role: "tabpanel", "aria-labelledby": `tab-${tab.id}`, hidden: true }, inst.el);
    view.append(root);
    instances.set(tab.id, { tab, inst, root });
  }
  active = instances.get(tab.id);
  for (const { tab: t, root } of instances.values()) root.hidden = t !== tab;
  for (const b of tabsNav.children) b.setAttribute("aria-selected", String(b.id === `tab-${tab.id}`));
  document.title = `${tab.title} · Server admin`;
  tick();
}

// ---- refresh loop -----------------------------------------------------------------------------

async function tick() {
  clearTimeout(timer);
  const mine = active;
  if (!document.hidden && mine) {
    try {
      const live = await api.live(mine.tab.id);
      lastOk = Date.now();
      if (mine === active) {                         // the user may have switched tab while this was in flight
        mine.inst.update(live);
        renderBanner(live.alerts || []);
        renderPowerBar(live.power);
      }
    } catch (e) {
      if (!(e instanceof AuthError)) console.warn("refresh failed:", e.message);
    }
  }
  timer = setTimeout(tick, REFRESH_MS);
}

function paintStatus() {
  const age = (Date.now() - lastOk) / 1000;
  const stale = !lastOk ? null : age > 15;
  statusDot.className = `dot ${stale === null ? "" : stale ? "crit" : "ok"}`;
  statusText.textContent = !lastOk ? "connecting…" : stale ? `no connection (${fmtAgo(lastOk / 1000)})` : age < 2 ? "live" : `updated ${Math.round(age)}s ago`;
}
setInterval(paintStatus, 1000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });

// ---- scheduled shutdown bar --------------------------------------------------------------------

let shutdown = null;          // {mode, at (ms on this computer's clock), waiting}
let countdownEl = null;

function renderPowerBar(power) {
  const s = power && power.scheduled;
  if (!s) { shutdown = null; powerBarEl.hidden = true; return; }
  const at = Date.now() + s.in_s * 1000;
  if (!shutdown || shutdown.mode !== s.mode || Math.abs(shutdown.at - at) > 3000) {     // rebuild only when something changed, so a click on Cancel is never lost
    shutdown = { mode: s.mode, at, waiting: shutdown ? shutdown.waiting : false };
    countdownEl = el("b", null, "");
    const cancel = ctx.session.admin.power
      ? el("button", { class: "btn small", type: "button", onclick: () => ctx.act("/api/power/cancel", {}) }, "Cancel") : null;
    powerBarEl.replaceChildren(el("div", { class: "statusbar-box warn" }, el("div", { class: "powerbar-row" },
      el("span", { class: "pill warn" }, s.label.toUpperCase()), el("span", null, "scheduled in ", countdownEl),
      s.requested_by ? el("span", { class: "muted" }, `requested by ${s.requested_by}`) : null, el("span", { class: "spacer" }), cancel)));
    powerBarEl.hidden = false;
  }
  paintCountdown();
}

function paintCountdown() {
  if (!shutdown) return;
  const left = Math.max(0, Math.round((shutdown.at - Date.now()) / 1000));
  countdownEl.textContent = fmtCountdown(left);
  if (left === 0 && !shutdown.waiting && ["reboot", "kexec"].includes(shutdown.mode)) {
    shutdown.waiting = true;
    waitForServer({ rebooting: true });
  }
}
setInterval(paintCountdown, 1000);

// ---- alert statusbar -----------------------------------------------------------------------------

let lastAlerts = [];

async function muteAlert(id, minutes) {
  try {
    const res = await api.post("/api/alerts/mute", minutes ? { id, minutes } : { id });
    renderBanner(res.alerts);
  } catch (e) { console.warn("mute failed:", e.message); }
}

async function unmuteAlert(id) {
  try {
    const res = await api.post("/api/alerts/unmute", { id });
    renderBanner(res.alerts);
  } catch (e) { console.warn("unmute failed:", e.message); }
}

function alertRow(a) {
  const now = Date.now() / 1000;
  const buttons = a.muted
    ? [el("button", { class: "btn small", type: "button", onclick: () => unmuteAlert(a.id) }, "Unmute")]
    : a.level === "info" ? [] : [
      el("button", { class: "btn small", type: "button", title: "Stop Telegram messages for an hour", onclick: () => muteAlert(a.id, 60) }, "Mute 1 h"),
      el("button", { class: "btn small", type: "button", title: "Stop Telegram messages for a day", onclick: () => muteAlert(a.id, 1440) }, "24 h"),
      el("button", { class: "btn small", type: "button", title: "No more messages about this until it is fixed", onclick: () => muteAlert(a.id, null) }, "Until fixed")];
  return el("li", { class: a.muted ? "muted" : "" },
    el("span", { class: `pill ${a.muted ? "" : a.level}` }, a.muted ? "muted" : a.level),
    el("span", null, a.title),
    a.detail ? el("small", null, a.detail) : null,
    a.since ? el("small", null, `for ${fmtDuration(now - a.since)}`) : null,
    el("span", { class: "spacer" }), buttons);
}

function renderBanner(alerts) {
  lastAlerts = alerts;
  if (!alerts.length) { statusbar.hidden = true; return; }
  const live = alerts.filter((a) => !a.muted), muted = alerts.filter((a) => a.muted);
  const count = (lvl) => live.filter((a) => a.level === lvl).length;
  const worst = count("crit") ? "crit" : count("warn") ? "warn" : live.length ? "info" : "muted";
  if (bannerOpen === null) bannerOpen = worst === "crit";
  const parts = [["crit", "critical"], ["warn", "warning"], ["info", "notice"]]
    .filter(([lvl]) => count(lvl)).map(([lvl, word]) => `${count(lvl)} ${word}${count(lvl) > 1 && word !== "critical" ? "s" : ""}`);
  if (muted.length) parts.push(`${muted.length} muted`);
  const head = el("button", { class: "statusbar-head", type: "button", "aria-expanded": String(bannerOpen),
    onclick: () => { bannerOpen = !bannerOpen; renderBanner(lastAlerts); } },
    el("span", { class: `pill ${worst === "muted" ? "" : worst}` }, worst === "crit" ? "ALERT" : worst === "warn" ? "WARNING" : worst === "info" ? "NOTICE" : "MUTED"),
    el("span", null, parts.join(" · ")), el("span", { class: "spacer" }), el("span", { class: "muted" }, bannerOpen ? "hide" : "show"));
  const list = bannerOpen ? el("ul", { class: "statusbar-list" }, [...live, ...muted].map(alertRow)) : null;
  statusbar.replaceChildren(el("div", { class: `statusbar-box ${worst === "muted" ? "info" : worst}` }, head, list));
  statusbar.hidden = false;
}

// ---- start ------------------------------------------------------------------------------------

document.getElementById("signout").addEventListener("click", () => api.signOut());

(async () => {
  try {
    const session = await api.session();
    ctx.thresholds = session.thresholds || {};
    ctx.session = { actions: !!session.actions, protected: session.protected || [], admin: session.admin || {} };
    document.getElementById("who").textContent = `${session.user} @ ${session.host}`;
    watchIdle(session.idle_s, { onIdle: () => api.signOut("idle"), ping: () => api.ping() });
  } catch (e) {
    if (!(e instanceof AuthError)) statusText.textContent = "cannot reach the server";
    return;
  }
  if (ctx.session.admin.users) TABS = [...BASE_TABS, users];
  buildTabs();
  window.addEventListener("hashchange", route);
  route();
})();
