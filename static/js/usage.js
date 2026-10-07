// What each row of the Overview's "Live usage" card shows: one headline value, an optional bar and
// one line of context. Pure functions (data in, plain object out), so they can be tested without a
// browser. `pct` null means "no bar for this row".

import { fmtBytes, fmtDuration, fmtNum, fmtPct, fmtRate, isNum, level } from "./util.js";

const IO_RULE = { warn: 70, crit: 90 };       // % of time the disk was busy
const PSI_RULE = { warn: 20, crit: 50 };      // % of the last 10 s that tasks spent waiting

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

export const summary = {
  cpu(d, th) {
    const t = d.total;
    if (!t) return { value: "…", sub: "measuring" };
    return { pct: t.busy, lvl: level(th.cpu_pct, t.busy), value: fmtPct(t.busy),
      sub: `load ${fmtNum(d.load[0], 2)} · ${plural(d.cores, "core")}` };
  },

  memory(d, th) {
    return { pct: d.used_pct, lvl: level(th.mem_avail_pct, d.available_pct, true), value: fmtPct(d.used_pct),
      sub: `${fmtBytes(d.used)} of ${fmtBytes(d.total)}` };
  },

  swap(d, th) {
    if (!d.swap_total) return { value: "none", sub: "no swap configured" };
    const moving = (d.swap_in_Bps || 0) + (d.swap_out_Bps || 0);
    return { pct: d.swap_used_pct, lvl: level(th.swap_pct, d.swap_used_pct), value: fmtPct(d.swap_used_pct),
      sub: `${fmtBytes(d.swap_used)} of ${fmtBytes(d.swap_total)}${moving > 0 ? ` · swapping ${fmtRate(moving)}` : ""}` };
  },

  disk(m, th) {
    if (m.error) return { value: "–", lvl: "warn", sub: `unavailable: ${m.error}` };
    return { pct: m.used_pct, lvl: level(th.disk_pct, m.used_pct), value: fmtPct(m.used_pct),
      sub: `${fmtBytes(m.used)} of ${fmtBytes(m.size)} · ${fmtBytes(m.available)} free` };
  },

  io(d) {
    const devices = d.devices || [];
    if (!devices.length) return { value: "–", sub: "no disks" };
    const busiest = Math.max(...devices.map((x) => x.busy_pct || 0));
    const sum = (key) => devices.reduce((total, x) => total + (x[key] || 0), 0);
    return { pct: busiest, lvl: level(IO_RULE, busiest), value: `${fmtPct(busiest, 0)} busy`,
      sub: `read ${fmtRate(sum("read_Bps"))} · write ${fmtRate(sum("write_Bps"))}` };
  },

  net(d) {
    const nics = d.interfaces || [];
    const sum = (key) => nics.reduce((total, x) => total + (x[key] || 0), 0);
    const up = nics.filter((x) => x.up).length;
    return { lvl: up < nics.length ? "warn" : "ok", value: `in ${fmtRate(sum("rx_Bps"))}`,
      sub: `out ${fmtRate(sum("tx_Bps"))} · ${up} of ${nics.length} up` };
  },

  /** `parts` is [[label, pressure]...] for cpu, memory and disk; the worst "some" average (10 s) wins. */
  pressure(parts) {
    const worst = parts.map(([label, p]) => [label, p && p.some ? p.some.avg10 : null]).filter(([, v]) => isNum(v))
      .reduce((best, cur) => (!best || cur[1] > best[1] ? cur : best), null);
    if (!worst) return { value: "–", sub: "not available" };
    return { pct: worst[1], lvl: level(PSI_RULE, worst[1]), value: fmtPct(worst[1]),
      sub: worst[1] < 1 ? "nothing is waiting" : `mostly waiting for ${worst[0]}` };
  },

  host(d) {
    return { value: `up ${fmtDuration(d.uptime_s)}`, sub: d.os || "" };
  },
};
