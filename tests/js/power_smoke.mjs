// The Power card against the fake DOM: what each button asks, that nothing is sent unless the typed confirmation
// is accepted, and that the progress of "restart all" is followed even when a poll or two fails.

import { installFakeDom } from "./fakedom.mjs";

let failed = 0;
const check = (ok, label) => { if (!ok) { console.log(`FAIL ${label}`); failed++; } };
installFakeDom();

const { powerCard, restartAllText, timing, TEXT } = await import("../../static/js/power.js");
const { AuthError } = await import("../../static/js/api.js");
timing.pollMs = 5;
const settle = (ms = 60) => new Promise((resolve) => setTimeout(resolve, ms));

const PLAN = { units: ["redis-server", "app1", "nginx"], excluded: ["tailscaled", "server-dashboard"], missing: ["smbd"] };
const POWER = { hostname: "myhost", scheduled: null, delays: [0, 60], restart: PLAN, job: null };

function makeCtx({ power = POWER, confirm = true, actResult = { ok: true }, jobs = [] } = {}) {
  const log = { confirms: [], acts: [], toasts: [], gets: [], refreshed: 0 };
  const queue = [...jobs];
  const ctx = {
    api: {
      async get(path) {
        log.gets.push(path);
        if (path === "/api/power") return typeof power === "function" ? power() : power;
        if (path.startsWith("/api/jobs/")) {
          const next = queue.length > 1 ? queue.shift() : queue[0];
          if (next instanceof Error) throw next;
          return next;
        }
        throw new Error(`unexpected GET ${path}`);
      },
    },
    confirmTyped: async (options) => { log.confirms.push(options); return typeof confirm === "function" ? confirm(options) : confirm; },
    act: async (path, body) => { log.acts.push([path, body]); return actResult; },
    toast: (text, kind) => log.toasts.push([text, kind]),
    refreshNow: () => { log.refreshed++; },
  };
  return { ctx, log };
}
const buttons = (card) => card.el.find((n) => n.tag === "button");
const byLabel = (card, label) => buttons(card).find((b) => b.textContent === label);
const job = (state, steps, detail = "") => ({ id: "j1", state, detail, steps });

// ---- the card and its three buttons
{
  const { ctx } = makeCtx();
  const card = powerCard(ctx);
  check(["Reboot in 1 minute", "Reboot now", "Restart all services"].every((l) => byLabel(card, l)), `three buttons: ${buttons(card).map((b) => b.textContent)}`);
  check(byLabel(card, "Reboot now").hasClass("danger") && byLabel(card, "Reboot in 1 minute").hasClass("danger"), "both reboots look dangerous");
}

// ---- reboot in a minute: asks with the typed hostname, sends only after confirmation
{
  const { ctx, log } = makeCtx({ confirm: false });
  const card = powerCard(ctx);
  byLabel(card, "Reboot in 1 minute").click(); await settle();
  const ask = log.confirms[0];
  check(ask && ask.word === "myhost" && ask.danger === true && ask.title === TEXT.rebootSoon.title, `reboot asks for the hostname: ${JSON.stringify(ask && ask.word)}`);
  check(ask.lines.join(" ").includes("Cancel") && ask.confirmLabel === "Reboot in 1 minute", "it explains the cancel window");
  check(log.acts.length === 0, "nothing is sent when the person says no");
}
{
  const { ctx, log } = makeCtx({ confirm: true });
  const card = powerCard(ctx);
  byLabel(card, "Reboot in 1 minute").click(); await settle();
  check(JSON.stringify(log.acts) === JSON.stringify([["/api/power/reboot", { confirm: "myhost", delay: 60 }]]), `confirmed reboot is sent: ${JSON.stringify(log.acts)}`);
}

// ---- reboot now: its own, stronger wording and delay 0
{
  const { ctx, log } = makeCtx();
  const card = powerCard(ctx);
  byLabel(card, "Reboot now").click(); await settle();
  check(log.confirms[0].title === TEXT.rebootNow.title && log.confirms[0].warning.join(" ").includes("no cancel"), "reboot now warns that there is no cancel");
  check(JSON.stringify(log.acts) === JSON.stringify([["/api/power/reboot", { confirm: "myhost", delay: 0 }]]), "and sends delay 0");
}

// ---- a shutdown that is already scheduled blocks a second request
{
  const { ctx, log } = makeCtx({ power: { ...POWER, scheduled: { label: "reboot", in_s: 30 } } });
  const card = powerCard(ctx);
  byLabel(card, "Reboot now").click(); await settle();
  check(log.confirms.length === 0 && log.acts.length === 0, "no dialog and no request while one is scheduled");
  check(log.toasts.some(([t]) => t.includes("already scheduled")), "it says why");
}

// ---- a server that cannot be asked
{
  const { ctx, log } = makeCtx({ power: () => { throw new Error("HTTP 500"); } });
  const card = powerCard(ctx);
  byLabel(card, "Reboot now").click(); await settle();
  check(log.confirms.length === 0 && log.acts.length === 0 && log.toasts.some(([t]) => t.includes("HTTP 500")), "a failed lookup shows the error and sends nothing");
}

// ---- restart all: the dialog lists the order and what is left alone; progress is followed to the end
{
  const running = job("running", [{ label: "restart redis-server", state: "ok", detail: "" }, { label: "restart app1", state: "running", detail: "" }]);
  const done = job("failed", [{ label: "restart redis-server", state: "ok", detail: "" }, { label: "restart app1", state: "failed", detail: "app1 is failed after the restart" },
    { label: "restart nginx", state: "ok", detail: "" }], "2 restarted, 1 failed (app1)");
  const { ctx, log } = makeCtx({ actResult: { ok: true, job: "j1" }, jobs: [running, new Error("nginx is restarting"), new Error("still"), done] });
  const card = powerCard(ctx);
  byLabel(card, "Restart all services").click(); await settle(200);
  const ask = log.confirms[0];
  check(ask.lines.join(" ").includes("redis-server, app1, nginx"), `the order is shown: ${ask.lines}`);
  check(ask.warning.join(" ").includes("tailscaled, server-dashboard") && ask.warning.join(" ").includes("smbd"), "what is left alone is shown");
  check(JSON.stringify(log.acts) === JSON.stringify([["/api/power/restart-all", { confirm: "myhost" }]]), "the request carries the typed hostname");
  const text = card.el.textContent;
  check(text.includes("failed") && text.includes("2 restarted, 1 failed (app1)") && text.includes("app1 is failed after the restart"), `the final state is shown: ${text}`);
  check(log.gets.filter((p) => p.startsWith("/api/jobs/")).length === 4, `polls survived two failures and stopped at the end: ${log.gets.length}`);
  check(log.refreshed >= 1, "the service list is refreshed afterwards");
  const before = log.gets.length;
  await settle(80);
  check(log.gets.length === before, "polling stops once the job is over");
}
{
  const { ctx, log } = makeCtx({ confirm: false });
  const card = powerCard(ctx);
  byLabel(card, "Restart all services").click(); await settle();
  check(log.acts.length === 0, "declining restart-all sends nothing");
}
{
  const { ctx, log } = makeCtx({ actResult: null });
  const card = powerCard(ctx);
  byLabel(card, "Restart all services").click(); await settle(100);
  check(!log.gets.some((p) => p.startsWith("/api/jobs/")), "a refused request is not followed");
}

// ---- reload in the middle of a restart: the card picks the job up again
{
  const { ctx, log } = makeCtx({ power: { ...POWER, job: "j1" }, jobs: [job("ok", [{ label: "restart nginx", state: "ok", detail: "" }], "1 restarted")] });
  const card = powerCard(ctx);
  await settle(100);
  check(card.el.textContent.includes("1 restarted") && log.gets.includes("/api/jobs/j1"), "a running job is resumed after a reload");
}
{
  const { ctx, log } = makeCtx({ power: { ...POWER, job: "j1" }, jobs: [job("running", [])] });
  const card = powerCard(ctx);
  byLabel(card, "Restart all services").click(); await settle();
  check(log.confirms.length === 0 && log.acts.length === 0 && log.toasts.some(([t]) => t.includes("already running")), "no second restart while one is running");
  card.el.detached = true;                                     // leaving the tab ends the polling
  await settle(50);
  const polls = log.gets.length;
  await settle(50);
  check(log.gets.length === polls, "polling stops when the card leaves the page");
}

// ---- signing out mid-poll ends the loop quietly
{
  const { ctx, log } = makeCtx({ power: { ...POWER, job: "j1" }, jobs: [new AuthError()] });
  powerCard(ctx);
  await settle(100);
  check(log.gets.filter((p) => p.startsWith("/api/jobs/")).length === 1 && log.toasts.length === 0, "a sign-out stops polling without a message");
}

// ---- the dialog text
{
  const t = restartAllText({ units: ["a", "b"], excluded: [], missing: [] });
  check(!t.warning.some((w) => w.includes("Not restarted")) && !t.warning.some((w) => w.includes("Skipped")), "no empty 'not restarted' or 'skipped' lines");
  check(t.warning.some((w) => w.includes("interrupted")), "the cost is always stated");
}

if (failed) { console.log(`${failed} check(s) failed`); process.exit(1); }
console.log("power smoke ok");
