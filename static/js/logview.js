import { api } from "./api.js";
import { el } from "./util.js";

/** Open the last log lines of a systemd unit or supervisor program in the shared dialog. */
export function openLogs(ctx, kind, name) {
  const pre = el("pre", null, "Loading…");
  const load = async () => {
    try {
      const res = await api.logs(kind, name, 300);
      pre.textContent = res.lines.length ? res.lines.join("\n") : "(no log lines)";
      pre.scrollTop = pre.scrollHeight;                    // newest at the bottom, like tail
    } catch (e) {
      pre.textContent = `Could not load the logs: ${e.message}`;
    }
  };
  ctx.openDialog(`Logs: ${name}`, pre, [el("button", { class: "btn small", type: "button", onclick: load }, "Refresh")]);
  load();
}
