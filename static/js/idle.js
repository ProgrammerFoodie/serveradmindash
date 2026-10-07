// Idle sign-out. "Inactive" means no real input (mouse, keys, touch, scroll) in this tab: the
// 5-second polling is not activity, so an unattended dashboard signs itself out. The server enforces
// the same limit; this side signs out on time and sends a heartbeat so the server sees the activity.

export const PING_MS = 15000;     // heartbeat at most this often while the person is active
export const CHECK_MS = 5000;
export const INPUT_EVENTS = ["pointerdown", "pointermove", "keydown", "wheel", "touchstart", "scroll"];

/** Pure bookkeeping, kept apart from the DOM so it can be tested. `now` is in milliseconds. */
export function idleTracker(idleMs, now = Date.now) {
  let lastInput = now(), lastPing = lastInput;
  const idle = () => now() - lastInput >= idleMs;
  return {
    idle,
    /** Input after the limit has passed does not revive the session: it is already over. */
    input() { if (!idle()) lastInput = now(); },
    /** True once per heartbeat period, and only if there has been input since the last one. */
    pingDue() { return !idle() && lastInput > lastPing && now() - lastPing >= PING_MS; },
    pinged() { lastPing = now(); },
    leftMs() { return Math.max(0, idleMs - (now() - lastInput)); },
  };
}

/** Start watching. `onIdle` runs once; `ping` sends the heartbeat. Returns a stop function. */
export function watchIdle(idleS, { onIdle, ping }) {
  if (!(idleS > 0)) return () => {};
  const tracker = idleTracker(idleS * 1000);
  let over = false;
  const check = () => {
    if (over) return;
    if (tracker.idle()) { over = true; onIdle(); return; }
    if (tracker.pingDue()) { tracker.pinged(); ping().catch(() => {}); }
  };
  const onInput = () => tracker.input();
  const onVisible = () => { if (!document.hidden) check(); };    // back from sleep or another tab
  for (const type of INPUT_EVENTS) window.addEventListener(type, onInput, { passive: true, capture: true });
  document.addEventListener("visibilitychange", onVisible);
  const timer = setInterval(check, CHECK_MS);
  return () => {
    clearInterval(timer);
    for (const type of INPUT_EVENTS) window.removeEventListener(type, onInput, { capture: true });
    document.removeEventListener("visibilitychange", onVisible);
  };
}
