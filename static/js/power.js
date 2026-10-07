// The Power card of the Services tab: reboot (now, or in a minute that can be cancelled) and "restart all services",
// which runs as a background job whose steps are shown here as they happen.

import { AuthError } from "./api.js";
import { badge, el, panel } from "./util.js";

export const timing = { pollMs: 1000 };           // a test sets this much lower
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const STATE = { running: ["running", "info"], ok: ["done", "ok"], failed: ["failed", "crit"] };

/** What the confirmation dialog tells the person, kept as data so it is easy to read and to test. */
export const TEXT = {
  rebootNow: {
    title: "Reboot the server now?",
    lines: ["The server restarts at once. This page reloads by itself when it is back, usually after one to two minutes."],
    warning: ["There is no cancel once it has started. Everything running on the server stops, websites included."],
    label: "Reboot now",
  },
  rebootSoon: {
    title: "Reboot the server in 1 minute?",
    lines: ["You get a minute to change your mind: a bar with the countdown and a Cancel button shows on every page. This page reloads by itself when the server is back."],
    warning: ["Everything running on the server stops when the minute is up, websites included."],
    label: "Reboot in 1 minute",
  },
};

export function restartAllText(plan) {
  return {
    title: "Restart every service?",
    lines: [`One after the other: ${plan.units.join(", ")}.`, "It carries on after a failure and shows the result of each. It takes about a minute."],
    warning: [
      ...(plan.excluded.length ? [`Not restarted, on purpose: ${plan.excluded.join(", ")}. Restarting them would cut you off from this page.`] : []),
      ...(plan.missing.length ? [`Skipped, not installed: ${plan.missing.join(", ")}.`] : []),
      "Websites on this server are interrupted while their services restart.",
    ],
    label: "Restart all",
  };
}

export function powerCard(ctx) {
  const api = ctx.api;
  const card = panel("Power");
  const status = el("p", { class: "sub" }, "Reboot the server, or restart every service in a safe order.");
  const jobBox = el("div", { class: "job", hidden: true });
  const button = (label, cls, onclick) => el("button", { class: `btn ${cls}`, type: "button", onclick }, label);
  const buttons = el("div", { class: "actions" },
    button("Reboot in 1 minute", "danger", () => reboot(60)),
    button("Reboot now", "danger", () => reboot(0)),
    button("Restart all services", "", restartAll));
  card.set(status, buttons, jobBox);

  async function info() {
    try { return await api.get("/api/power"); } catch (e) { if (!(e instanceof AuthError)) ctx.toast(e.message, "bad"); return null; }
  }

  async function reboot(delay) {
    const power = await info();
    if (!power) return;
    if (power.scheduled) { ctx.toast(`A ${power.scheduled.label} is already scheduled. Cancel it first (the bar at the top).`, "warn"); return; }
    const text = delay === 0 ? TEXT.rebootNow : TEXT.rebootSoon;
    const ok = await ctx.confirmTyped({ title: text.title, lines: text.lines, warning: text.warning, word: power.hostname, confirmLabel: text.label, danger: true });
    if (ok) await ctx.act("/api/power/reboot", { confirm: power.hostname, delay });
  }

  async function restartAll() {
    const power = await info();
    if (!power) return;
    if (power.job) { ctx.toast("A restart is already running.", "warn"); follow(power.job); return; }
    const text = restartAllText(power.restart);
    const ok = await ctx.confirmTyped({ title: text.title, lines: text.lines, warning: text.warning, word: power.hostname, confirmLabel: text.label, danger: true });
    if (!ok) return;
    const result = await ctx.act("/api/power/restart-all", { confirm: power.hostname });
    if (result && result.job) follow(result.job);
  }

  function render(job) {
    const [word, kind] = STATE[job.state] || [job.state, ""];
    jobBox.hidden = false;
    jobBox.replaceChildren(
      el("div", { class: "rowline" }, el("b", null, "Restart all services"), badge(word, kind)),
      job.detail ? el("p", { class: job.state === "failed" ? "err" : "sub" }, job.detail) : null,
      el("ul", { class: "job-steps" }, job.steps.map((s) => {
        const [label, cls] = STATE[s.state] || [s.state, ""];
        return el("li", null, badge(label, cls), " ", s.label, s.detail ? el("div", { class: "sub" }, s.detail) : null);
      })));
  }

  /** Follow a job until it ends. Polls survive failures: nginx is one of the services being restarted. */
  async function follow(id) {
    let failures = 0;
    while (card.el.isConnected) {
      try {
        const job = await api.get(`/api/jobs/${id}`);
        failures = 0;
        render(job);
        if (job.state !== "running") { ctx.refreshNow(); return; }
      } catch (e) {
        if (e instanceof AuthError) return;
        if (++failures > 40) { ctx.toast("Lost track of the restart. Look at the service list below.", "warn"); return; }
      }
      await wait(timing.pollMs);
    }
  }

  // A page reload in the middle of a restart picks the job up again.
  api.get("/api/power").then((power) => { if (power && power.job) follow(power.job); }).catch(() => { /* not important enough to show */ });

  return { el: card.el, follow, reboot, restartAll };
}
