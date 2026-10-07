import { ChartGroup, LineChart } from "../chart.js";
import { badge, bar, el, fmtBytes, fmtDuration, fmtNum, fmtPct, fmtRate, isNum, kv, level, paint, store } from "../util.js";
import { summary } from "../usage.js";
import { makeSortable } from "../dragsort.js";
import { masonry } from "../masonry.js";

// The Overview is a board of cards. "Live usage" lists every resource on one compact line; opening a row
// shows that resource's card (details and history graphs) next to it. Cards can be dragged into any order.

const OPEN_KEY = "overview_open", ORDER_KEY = "overview_order";
const DEFAULT_OPEN = ["cpu", "memory"];          // what a first visit shows: the two things people look at first

function loadList(key, fallback) {
  try {
    const saved = JSON.parse(store.get(key, "null"));
    return Array.isArray(saved) ? saved.filter((x) => typeof x === "string") : fallback;
  } catch { return fallback; }
}

/** Move the bar without replacing it, so the width change animates. */
function setBar(barEl, pct, lvl) {
  barEl.hidden = !isNum(pct);
  barEl.className = `bar thin usage-bar ${lvl || "ok"}`;
  barEl.firstChild.style.width = `${Math.max(0, Math.min(100, isNum(pct) ? pct : 0))}%`;
  barEl.setAttribute("aria-label", fmtPct(pct, 0));
}

/** Fill a row from a live section: waiting, collector error, or its summary. */
function rowFrom(item, section, render) {
  if (!section) return item.set({ value: "…", sub: "waiting for data" });
  if (section.data && section.data.error) return item.set({ value: "–", lvl: "warn", sub: section.data.error });
  try { item.set(render(section.data)); } catch (e) { console.error(e); item.set({ value: "–", lvl: "warn", sub: "unexpected data" }); }
}

const grip = (title) => el("button", { class: "grip", type: "button", title: "Drag to move, or use the arrow keys",
  "aria-label": `Move the ${title} card. Drag it, or press an arrow key.` }, "⠿");

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
    const group = new ChartGroup(ctx);
    const chart = (opts) => new LineChart({ bare: true, ...opts });
    const open = new Set(loadList(OPEN_KEY, DEFAULT_OPEN));
    const items = new Map();                       // key → row + card; disks are added as they appear
    const board = el("div", { class: "board" });
    masonry(board);
    const list = el("div", { class: "usage-list" });
    let sortable = null;

    const allOpen = () => [...items.values()].every((i) => i.open);
    const toggleAll = el("button", { class: "btn small", type: "button", onclick: () => {
      const target = !allOpen();
      [...items.values()].forEach((i) => i.setOpen(target));
    } }, "Open all");
    const syncToggleAll = () => { toggleAll.textContent = allOpen() ? "Close all" : "Open all"; };

    /** One resource: a row in the Live usage list and a card (details + graphs) that is hidden until the row is opened. */
    function makeItem(key, label) {
      let isOpen = open.has(key);
      const charts = [];
      const detail = el("div", { class: "card-detail" });
      const chartsEl = el("div", { class: "card-charts" });
      const hide = el("button", { class: "btn small", type: "button", onclick: () => toggle(false) }, "Hide");
      const card = el("section", { class: "card drag-card", "data-key": key, id: `ov-${key.replace(/[^a-z0-9]+/gi, "-")}` },
        el("h3", null, grip(label), el("span", { class: "card-title" }, label), el("span", { class: "grow" }), hide), chartsEl, detail);

      const value = el("span", { class: "usage-value" }, "…"), sub = el("span", { class: "usage-sub" });
      const barEl = bar(0, "ok", true);
      barEl.classList.add("usage-bar");
      barEl.hidden = true;
      const head = el("button", { class: "usage-head", type: "button", "aria-controls": card.id, onclick: () => toggle() },
        el("span", { class: "usage-name" }, label), barEl, value, sub, el("span", { class: "usage-chev", "aria-hidden": "true" }, "›"));
      const row = el("div", { class: "usage-item" }, head);

      function render() {
        head.setAttribute("aria-expanded", String(isOpen));
        card.hidden = !isOpen;
        row.classList.toggle("open", isOpen);
      }
      function toggle(next = !isOpen, byPerson = true) {
        isOpen = next;
        render();
        if (isOpen) { open.add(key); charts.forEach((c) => group.add(c)); group.refresh(); } else { open.delete(key); charts.forEach((c) => group.remove(c)); }
        store.set(OPEN_KEY, JSON.stringify([...open]));
        syncToggleAll();
        // On a phone the new card lands below the whole list: bring it into view.
        if (isOpen && byPerson && card.scrollIntoView && card.offsetWidth > board.clientWidth * 0.6) card.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
      render();

      const item = {
        key, row, card,
        detail: { set: (...nodes) => detail.replaceChildren(...nodes.flat(Infinity).filter((n) => n != null && n !== false)) },
        get open() { return isOpen; },
        setOpen(next) { if (next !== isOpen) toggle(next, false); },
        addChart(c) { charts.push(c); chartsEl.append(c.el); if (isOpen) group.add(c); },
        set({ pct = null, lvl = "ok", value: text = "–", sub: line = "" }) {
          setBar(barEl, pct, lvl);
          value.textContent = text;
          value.className = `usage-value ${lvl}`;
          sub.textContent = line;
        },
      };
      items.set(key, item);
      return item;
    }

    const cpuI = makeItem("cpu", "CPU"), memI = makeItem("memory", "Memory"), swapI = makeItem("swap", "Swap");
    const ioI = makeItem("io", "Disk activity"), netI = makeItem("net", "Network"), psiI = makeItem("psi", "Pressure"), hostI = makeItem("host", "Host");
    const natural = () => ["cpu", "memory", "swap", ...[...items.keys()].filter((k) => k.startsWith("disk:")), "io", "net", "psi", "host"];

    list.append(...[cpuI, memI, swapI, ioI, netI, psiI, hostI].map((i) => i.row));    // disk rows go before the Disk activity row
    board.append(...[cpuI, memI, swapI, ioI, netI, psiI, hostI].map((i) => i.card));

    sortable = makeSortable(board, {
      load: () => loadList(ORDER_KEY, []),
      save: (keys) => store.set(ORDER_KEY, JSON.stringify(keys)),
      defaults: natural,
    });
    sortable.reapply();
    syncToggleAll();

    cpuI.addChart(chart({ title: "CPU", unit: "percent", minMax: 10, series: [
      { metric: "cpu.busy", label: "busy", color: 1 }, { metric: "cpu.iowait", label: "iowait", color: 4 }, { metric: "cpu.steal", label: "steal", color: 5 }] }));
    cpuI.addChart(chart({ title: "Load average", unit: "number", minMax: 1, series: [
      { metric: "load.1", label: "1 min", color: 1 }, { metric: "load.5", label: "5 min", color: 3 }] }));
    memI.addChart(chart({ title: "Memory and swap used", unit: "percent", yMax: 100, series: [
      { metric: "mem.used_pct", label: "memory", color: 1 }, { metric: "mem.swap_used_pct", label: "swap", color: 3 }] }));
    swapI.addChart(chart({ title: "Swap traffic", unit: "rate", series: [
      { metric: "mem.swap_in_Bps", label: "swap in", color: 4 }, { metric: "mem.swap_out_Bps", label: "swap out", color: 3 }],
      note: "Constant swapping means RAM is too small for the workload." }));
    psiI.addChart(chart({ title: "Pressure (stall time)", unit: "percent", minMax: 5, series: [
      { metric: "psi.cpu_some", label: "cpu", color: 1 }, { metric: "psi.mem_some", label: "memory", color: 3 },
      { metric: "psi.mem_full", label: "memory stalled", color: 4 }, { metric: "psi.io_some", label: "io", color: 6 },
      { metric: "psi.io_full", label: "io stalled", color: 5 }],
      note: "Share of the last 10 s in which tasks were waiting for the resource." }));
    let ioCharts = false, netCharts = false;   // these need the disk and interface names, which the first live data supplies

    const livePanel = el("section", { class: "card live-panel" },
      el("div", { class: "panel-head" },
        el("h3", null, "Live usage"), el("span", { class: "muted hint" }, "open a row for details and graphs · drag ⠿ to rearrange"), el("span", { class: "spacer" }),
        toggleAll,
        el("button", { class: "btn small", type: "button", onclick: () => sortable.reset() }, "Reset order"),
        ctx.rangePicker()),
      list);

    return {
      el: el("div", { class: "rows" }, livePanel, board),
      onRange() { group.refresh(true); },
      update(live) {
        const s = live.sections;

        if (!ioCharts && s.disk_io?.data?.devices?.length) {
          ioCharts = true;
          const devices = s.disk_io.data.devices.map((d) => d.device);
          ioI.addChart(chart({ title: "Disk busy", unit: "percent", yMax: 100, series: devices.map((d, i) => ({ metric: `io.${d}.busy_pct`, label: d, color: i + 1 })) }));
          ioI.addChart(chart({ title: "Disk throughput", unit: "rate", series: devices.flatMap((d, i) => [
            { metric: `io.${d}.read_Bps`, label: `${d} read`, color: (i * 2) % 6 + 1 }, { metric: `io.${d}.write_Bps`, label: `${d} write`, color: (i * 2 + 1) % 6 + 1 }]) }));
        }
        if (!netCharts && s.net_io?.data?.interfaces?.length) {
          netCharts = true;
          const ifaces = s.net_io.data.interfaces.map((i) => i.name);
          netI.addChart(chart({ title: "Network", unit: "rate", series: ifaces.flatMap((n, i) => [
            { metric: `net.${n}.rx_Bps`, label: `${n} in`, color: (i * 2) % 6 + 1 }, { metric: `net.${n}.tx_Bps`, label: `${n} out`, color: (i * 2 + 1) % 6 + 1 }]) }));
        }

        // ---- the rows
        rowFrom(cpuI, s.cpu, (d) => summary.cpu(d, th()));
        rowFrom(memI, s.memory, (d) => summary.memory(d, th()));
        rowFrom(swapI, s.memory, (d) => summary.swap(d, th()));
        rowFrom(ioI, s.disk_io, summary.io);
        rowFrom(netI, s.net_io, summary.net);
        rowFrom(hostI, s.system, summary.host);
        const live3 = [s.cpu, s.memory, s.disk_io];
        if (live3.every((x) => x && x.data && !x.data.error)) {
          psiI.set(summary.pressure([["CPU", s.cpu.data.pressure], ["memory", s.memory.data.pressure], ["disk", s.disk_io.data.pressure]]));
        } else psiI.set({ value: "…", sub: "waiting for data" });

        // ---- the cards
        paint(hostI.detail, s.system, (d) => {
          const sync = d.time_sync && !d.time_sync.error ? d.time_sync : null;
          return kv([
            ["Hostname", d.hostname], ["System", d.os], ["Kernel", `${d.kernel} (${d.arch})`],
            ["Uptime", fmtDuration(d.uptime_s)],
            ["CPU", `${d.cpu_count} × ${d.cpu_model}`], ["Virtualization", d.virtualization],
            ["Clock", sync ? [badge(sync.synced ? "in sync" : sync.leap_status || "not synced", sync.synced ? "ok" : "warn"),
              isNum(sync.offset_s) ? ` ${fmtNum(sync.offset_s * 1000, 2)} ms off` : ""] : "unknown"],
            ["Time zone", d.timezone]]);
        });

        paint(cpuI.detail, s.cpu, (d) => {
          const t = d.total;
          return [
            t ? el("div", { class: "sub" }, `user ${fmtPct(t.user + t.nice, 0)} · system ${fmtPct(t.system + t.irq + t.softirq, 0)} · iowait ${fmtPct(t.iowait, 0)} · steal ${fmtPct(t.steal, 0)}`) : null,
            el("div", { class: "rows" }, kv([
              ["Load 1 / 5 / 15 min", d.load.map((x) => fmtNum(x, 2)).join(" · ")],
              ["Load per core", [fmtNum(d.load_per_core, 2), " ", badge(`${d.cores} core${d.cores > 1 ? "s" : ""}`)]],
              ["Tasks running / blocked", `${d.tasks_running} / ${d.tasks_blocked} of ${d.tasks_total}`],
              ["Context switches", isNum(d.ctxt_per_s) ? `${fmtNum(d.ctxt_per_s)}/s` : undefined],
              ["Process starts", isNum(d.forks_per_s) ? `${fmtNum(d.forks_per_s, 1)}/s` : undefined]]),
              d.per_core && d.per_core.length > 1 ? d.per_core.map((c, i) => el("div", { class: "rowline" }, el("span", { class: "muted" }, `core ${i}`), bar(c.busy, level(th().cpu_pct, c.busy), true), fmtPct(c.busy, 0))) : null)];
        });

        paint(memI.detail, s.memory, (d) => kv([
          ["Available", `${fmtBytes(d.available)} (${fmtPct(d.available_pct, 0)})`], ["Page cache", fmtBytes(d.cached)],
          ["Buffers", fmtBytes(d.buffers)], ["Kernel slab", fmtBytes(d.slab_reclaimable + d.slab_unreclaimable)],
          ["Programs (anon)", fmtBytes(d.anon)], ["Shared", fmtBytes(d.shmem)], ["Dirty / writeback", `${fmtBytes(d.dirty)} / ${fmtBytes(d.writeback)}`],
          ["Committed", `${fmtBytes(d.committed)} of ${fmtBytes(d.commit_limit)}`]]));

        paint(swapI.detail, s.memory, (d) => [
          kv([["Swapping in", fmtRate(d.swap_in_Bps)], ["Swapping out", fmtRate(d.swap_out_Bps)],
            ["Major page faults", isNum(d.major_faults_per_s) ? `${fmtNum(d.major_faults_per_s, 1)}/s` : undefined],
            ["Out-of-memory kills", [fmtNum(d.oom_kills_total), d.oom_kills_total ? " " : "", d.oom_kills_total ? badge("since boot", "warn") : ""]]]),
          ...d.swaps.map((x) => el("div", null, el("div", { class: "rowline" }, el("span", { class: "mono" }, x.name), el("span", { class: "muted" }, `${x.type} · ${fmtBytes(x.used)} / ${fmtBytes(x.size)}`)), bar(x.used_pct, "ok", true)))]);

        paint(ioI.detail, s.disk_io, (d) => [
          ...d.devices.map((x) => el("div", null,
            el("div", { class: "rowline" }, el("b", null, x.device, " ", badge(x.role || "unused")), el("span", { class: level({ warn: 70, crit: 90 }, x.busy_pct) !== "ok" ? "err" : "muted" }, `${fmtPct(x.busy_pct, 0)} busy`)),
            bar(x.busy_pct, level({ warn: 70, crit: 90 }, x.busy_pct), true),
            el("div", { class: "sub" }, `read ${fmtRate(x.read_Bps)} · write ${fmtRate(x.write_Bps)} · ${fmtNum(x.read_iops + x.write_iops, 0)} IO/s · wait ${isNum(x.await_ms) ? fmtNum(x.await_ms, 1) + " ms" : "–"}`))),
          el("div", { class: "sub" }, `Waiting for disk: ${fmtPct(d.pressure.some.avg10, 0)} of the last 10 s`)]);

        paint(netI.detail, s.net_io, (d) => d.interfaces.map((i) => el("div", null,
          el("div", { class: "rowline" }, el("b", null, i.name), badge(i.up ? "up" : "down", i.up ? "ok" : "crit")),
          el("div", { class: "sub" }, `in ${fmtRate(i.rx_Bps)} · out ${fmtRate(i.tx_Bps)} · ${fmtNum(i.rx_pps, 0)} / ${fmtNum(i.tx_pps, 0)} pkt/s`),
          i.rx_errors + i.tx_errors + i.rx_drops + i.tx_drops ? el("div", { class: "err" }, `errors ${i.rx_errors + i.tx_errors}, dropped ${i.rx_drops + i.tx_drops} since boot`) : null)));

        if (live3.every((x) => x && x.data && !x.data.error)) {
          psiI.detail.set(el("div", { class: "sub" }, "average over 10 s · 1 min · 5 min"),
            el("div", { class: "rows" },
              el("div", null, el("b", null, "CPU"), pressureRows(s.cpu.data.pressure)[0]),
              el("div", null, el("b", null, "Memory"), pressureRows(s.memory.data.pressure)),
              el("div", null, el("b", null, "Disk IO"), pressureRows(s.disk_io.data.pressure))));
        } else paint(psiI.detail, null);

        // one row and card per mounted filesystem, added as they appear
        const mounts = s.disks && s.disks.data && !s.disks.data.error ? s.disks.data.mounts : [];
        let added = false;
        for (const m of mounts) {
          const key = `disk:${m.mount}`;
          let item = items.get(key);
          if (!item) {
            item = makeItem(key, `Disk ${m.mount}`);
            list.insertBefore(item.row, ioI.row);
            board.insertBefore(item.card, ioI.card);
            if (!m.error) item.addChart(chart({ title: "Space used", unit: "percent", yMax: 100, series: [{ metric: `fs.${m.mount}.used_pct`, label: m.mount, color: 1 }] }));
            added = true;
          }
          item.set(summary.disk(m, th()));
          if (m.error) { item.detail.set(el("p", { class: "err" }, `Unavailable: ${m.error}`)); continue; }
          const ilvl = level(th().inode_pct, m.inodes_pct);
          item.detail.set(kv([["Device", `${m.device} (${m.fstype})`], ["Inodes", [`${fmtPct(m.inodes_pct)} of ${fmtNum(m.inodes_total)}`, " ", ilvl !== "ok" ? badge("high", ilvl) : ""]],
            ["Mode", m.readonly ? badge("read-only", "warn") : "read-write"]]));
        }
        if (added) { sortable.reapply(); syncToggleAll(); }
        group.refresh();
      },
    };
  },
};
