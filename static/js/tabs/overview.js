import { ChartGroup, LineChart } from "../chart.js";
import { badge, bar, el, fmtBytes, fmtDuration, fmtNum, fmtPct, fmtRate, isNum, kv, level, paint, panel } from "../util.js";

const big = (value, lvl, unit) => el("div", { class: `big ${lvl}` }, value, unit ? el("small", null, unit) : null);

function pressureRows(p) {
  const row = (label, kind) => p && p[kind] ? el("div", { class: "rowline" },
    el("span", { class: "muted" }, label),
    el("span", null, `${fmtPct(p[kind].avg10)} · ${fmtPct(p[kind].avg60)} · ${fmtPct(p[kind].avg300)}`)) : null;
  return [row("waiting (some)", "some"), row("stalled (full)", "full")];
}

export default {
  id: "overview",
  title: "Overview",

  create(ctx) {
    const th = () => ctx.thresholds;
    const host = panel("Host"), cpu = panel("CPU"), mem = panel("Memory"), swap = panel("Swap");
    const io = panel("Disk activity"), net = panel("Network"), psi = panel("Pressure");
    const diskCards = new Map();              // mount → panel
    const cards = el("div", { class: "grid" }, host.el, cpu.el, mem.el, swap.el, io.el, net.el, psi.el);   // disk-space cards are inserted before `io`
    const group = new ChartGroup(ctx);
    const chartsEl = el("div", { class: "grid wide" });
    let chartsBuilt = false;

    const root = el("div", { class: "rows" },
      cards,
      el("div", { class: "toolbar" }, el("h2", { class: "brand" }, "History"), el("span", { class: "spacer" }), ctx.rangePicker()),
      chartsEl);

    /** Charts are created once the first live data names the disks and interfaces to chart. */
    function buildCharts(live) {
      const devices = live.sections.disk_io?.data?.devices?.map((d) => d.device) || [];
      const ifaces = live.sections.net_io?.data?.interfaces?.map((i) => i.name) || [];
      const mounts = live.sections.disks?.data?.mounts?.filter((m) => !m.error).map((m) => m.mount) || [];
      const add = (c) => { group.add(c); chartsEl.append(c.el); };
      add(new LineChart({ title: "CPU", unit: "percent", minMax: 10, series: [
        { metric: "cpu.busy", label: "busy", color: 1 }, { metric: "cpu.iowait", label: "iowait", color: 4 }, { metric: "cpu.steal", label: "steal", color: 5 }] }));
      add(new LineChart({ title: "Load average", unit: "number", minMax: 1, series: [
        { metric: "load.1", label: "1 min", color: 1 }, { metric: "load.5", label: "5 min", color: 3 }] }));
      add(new LineChart({ title: "Memory and swap used", unit: "percent", yMax: 100, series: [
        { metric: "mem.used_pct", label: "memory", color: 1 }, { metric: "mem.swap_used_pct", label: "swap", color: 3 }] }));
      add(new LineChart({ title: "Swap traffic", unit: "rate", series: [
        { metric: "mem.swap_in_Bps", label: "swap in", color: 4 }, { metric: "mem.swap_out_Bps", label: "swap out", color: 3 }],
        note: "Constant swapping means RAM is too small for the workload." }));
      if (devices.length) {
        add(new LineChart({ title: "Disk busy", unit: "percent", yMax: 100, series: devices.map((d, i) => ({ metric: `io.${d}.busy_pct`, label: d, color: i + 1 })) }));
        add(new LineChart({ title: "Disk throughput", unit: "rate", series: devices.flatMap((d, i) => [
          { metric: `io.${d}.read_Bps`, label: `${d} read`, color: (i * 2) % 6 + 1 }, { metric: `io.${d}.write_Bps`, label: `${d} write`, color: (i * 2 + 1) % 6 + 1 }]) }));
      }
      if (ifaces.length) {
        add(new LineChart({ title: "Network", unit: "rate", series: ifaces.flatMap((n, i) => [
          { metric: `net.${n}.rx_Bps`, label: `${n} in`, color: (i * 2) % 6 + 1 }, { metric: `net.${n}.tx_Bps`, label: `${n} out`, color: (i * 2 + 1) % 6 + 1 }]) }));
      }
      add(new LineChart({ title: "Pressure (stall time)", unit: "percent", minMax: 5, series: [
        { metric: "psi.cpu_some", label: "cpu", color: 1 }, { metric: "psi.mem_some", label: "memory", color: 3 },
        { metric: "psi.mem_full", label: "memory stalled", color: 4 }, { metric: "psi.io_some", label: "io", color: 6 },
        { metric: "psi.io_full", label: "io stalled", color: 5 }],
        note: "Share of the last 10 s in which tasks were waiting for the resource." }));
      if (mounts.length) {
        add(new LineChart({ title: "Disk space used", unit: "percent", yMax: 100, series: mounts.map((m, i) => ({ metric: `fs.${m}.used_pct`, label: m, color: i + 1 })) }));
      }
    }

    return {
      el: root,
      onRange() { group.refresh(true); },
      update(live) {
        const s = live.sections;
        if (!chartsBuilt && (s.disk_io || s.net_io)) { chartsBuilt = true; buildCharts(live); }
        group.refresh();

        paint(host, s.system, (d) => {
          const sync = d.time_sync && !d.time_sync.error ? d.time_sync : null;
          return kv([
            ["Hostname", d.hostname], ["System", d.os], ["Kernel", `${d.kernel} (${d.arch})`],
            ["Uptime", fmtDuration(d.uptime_s)],
            ["CPU", `${d.cpu_count} × ${d.cpu_model}`], ["Virtualization", d.virtualization],
            ["Clock", sync ? [badge(sync.synced ? "in sync" : sync.leap_status || "not synced", sync.synced ? "ok" : "warn"),
              isNum(sync.offset_s) ? ` ${fmtNum(sync.offset_s * 1000, 2)} ms off` : ""] : "unknown"],
            ["Time zone", d.timezone]]);
        });

        paint(cpu, s.cpu, (d) => {
          const t = d.total, lvl = level(th().cpu_pct, t && t.busy);
          return [
            t ? big(fmtPct(t.busy), lvl) : big("…", "", "measuring"), t ? bar(t.busy, lvl) : null,
            t ? el("div", { class: "sub" }, `user ${fmtPct(t.user + t.nice, 0)} · system ${fmtPct(t.system + t.irq + t.softirq, 0)} · iowait ${fmtPct(t.iowait, 0)} · steal ${fmtPct(t.steal, 0)}`) : null,
            el("div", { class: "rows" }, kv([
              ["Load 1 / 5 / 15 min", d.load.map((x) => fmtNum(x, 2)).join(" · ")],
              ["Load per core", [fmtNum(d.load_per_core, 2), " ", badge(`${d.cores} core${d.cores > 1 ? "s" : ""}`)]],
              ["Tasks running / blocked", `${d.tasks_running} / ${d.tasks_blocked} of ${d.tasks_total}`],
              ["Context switches", isNum(d.ctxt_per_s) ? `${fmtNum(d.ctxt_per_s)}/s` : undefined],
              ["Process starts", isNum(d.forks_per_s) ? `${fmtNum(d.forks_per_s, 1)}/s` : undefined]]),
              d.per_core && d.per_core.length > 1 ? d.per_core.map((c, i) => el("div", { class: "rowline" }, el("span", { class: "muted" }, `core ${i}`), bar(c.busy, level(th().cpu_pct, c.busy), true), fmtPct(c.busy, 0))) : null)];
        });

        paint(mem, s.memory, (d) => {
          const lvl = level(th().mem_avail_pct, d.available_pct, true);
          return [
            big(fmtPct(d.used_pct), lvl), bar(d.used_pct, lvl),
            el("div", { class: "sub" }, `${fmtBytes(d.used)} of ${fmtBytes(d.total)} in use`),
            kv([["Available", `${fmtBytes(d.available)} (${fmtPct(d.available_pct, 0)})`], ["Page cache", fmtBytes(d.cached)],
              ["Buffers", fmtBytes(d.buffers)], ["Kernel slab", fmtBytes(d.slab_reclaimable + d.slab_unreclaimable)],
              ["Programs (anon)", fmtBytes(d.anon)], ["Shared", fmtBytes(d.shmem)], ["Dirty / writeback", `${fmtBytes(d.dirty)} / ${fmtBytes(d.writeback)}`],
              ["Committed", `${fmtBytes(d.committed)} of ${fmtBytes(d.commit_limit)}`]])];
        });

        paint(swap, s.memory, (d) => {
          const lvl = level(th().swap_pct, d.swap_used_pct);
          return [
            big(fmtPct(d.swap_used_pct), lvl), bar(d.swap_used_pct, lvl),
            el("div", { class: "sub" }, `${fmtBytes(d.swap_used)} of ${fmtBytes(d.swap_total)}`),
            kv([["Swapping in", fmtRate(d.swap_in_Bps)], ["Swapping out", fmtRate(d.swap_out_Bps)],
              ["Major page faults", isNum(d.major_faults_per_s) ? `${fmtNum(d.major_faults_per_s, 1)}/s` : undefined],
              ["Out-of-memory kills", [fmtNum(d.oom_kills_total), d.oom_kills_total ? " " : "", d.oom_kills_total ? badge("since boot", "warn") : ""]]]),
            ...d.swaps.map((x) => el("div", null, el("div", { class: "rowline" }, el("span", { class: "mono" }, x.name), el("span", { class: "muted" }, `${x.type} · ${fmtBytes(x.used)} / ${fmtBytes(x.size)}`)), bar(x.used_pct, "ok", true)))];
        });

        paint(io, s.disk_io, (d) => [
          ...d.devices.map((x) => el("div", null,
            el("div", { class: "rowline" }, el("b", null, x.device, " ", badge(x.role || "unused")), el("span", { class: level({ warn: 70, crit: 90 }, x.busy_pct) !== "ok" ? "err" : "muted" }, `${fmtPct(x.busy_pct, 0)} busy`)),
            bar(x.busy_pct, level({ warn: 70, crit: 90 }, x.busy_pct), true),
            el("div", { class: "sub" }, `read ${fmtRate(x.read_Bps)} · write ${fmtRate(x.write_Bps)} · ${fmtNum(x.read_iops + x.write_iops, 0)} IO/s · wait ${isNum(x.await_ms) ? fmtNum(x.await_ms, 1) + " ms" : "–"}`))),
          el("div", { class: "sub" }, `Waiting for disk: ${fmtPct(d.pressure.some.avg10, 0)} of the last 10 s`)]);

        paint(net, s.net_io, (d) => d.interfaces.map((i) => el("div", null,
          el("div", { class: "rowline" }, el("b", null, i.name), badge(i.up ? "up" : "down", i.up ? "ok" : "crit")),
          el("div", { class: "sub" }, `in ${fmtRate(i.rx_Bps)} · out ${fmtRate(i.tx_Bps)} · ${fmtNum(i.rx_pps, 0)} / ${fmtNum(i.tx_pps, 0)} pkt/s`),
          i.rx_errors + i.tx_errors + i.rx_drops + i.tx_drops ? el("div", { class: "err" }, `errors ${i.rx_errors + i.tx_errors}, dropped ${i.rx_drops + i.tx_drops} since boot`) : null)));

        const live3 = [s.cpu, s.memory, s.disk_io];
        if (live3.every((x) => x && x.data && !x.data.error)) {
          psi.set(el("div", { class: "sub" }, "average over 10 s · 1 min · 5 min"),
            el("div", { class: "rows" },
              el("div", null, el("b", null, "CPU"), pressureRows(s.cpu.data.pressure)[0]),
              el("div", null, el("b", null, "Memory"), pressureRows(s.memory.data.pressure)),
              el("div", null, el("b", null, "Disk IO"), pressureRows(s.disk_io.data.pressure))));
        } else paint(psi, null);

        // one card per mounted filesystem, inserted as they appear
        const mounts = s.disks && s.disks.data && !s.disks.data.error ? s.disks.data.mounts : [];
        for (const m of mounts) {
          let p = diskCards.get(m.mount);
          if (!p) {
            p = panel(`Disk ${m.mount}`);
            diskCards.set(m.mount, p);
            cards.insertBefore(p.el, io.el);
          }
          if (m.error) { p.set(el("p", { class: "err" }, `Unavailable: ${m.error}`)); continue; }
          const lvl = level(th().disk_pct, m.used_pct), ilvl = level(th().inode_pct, m.inodes_pct);
          p.set(big(fmtPct(m.used_pct), lvl), bar(m.used_pct, lvl),
            el("div", { class: "sub" }, `${fmtBytes(m.used)} of ${fmtBytes(m.size)} used, ${fmtBytes(m.available)} free`),
            kv([["Device", `${m.device} (${m.fstype})`], ["Inodes", [`${fmtPct(m.inodes_pct)} of ${fmtNum(m.inodes_total)}`, " ", ilvl !== "ok" ? badge("high", ilvl) : ""]],
              ["Mode", m.readonly ? badge("read-only", "warn") : "read-write"]]));
        }
      },
    };
  },
};
