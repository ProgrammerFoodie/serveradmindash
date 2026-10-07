// Checks the pure helper functions of the front end with plain Node (no browser, no packages).
import { niceBytesMax, niceMax, nearest } from "../../static/js/chart.js";
import { fmtAgo, fmtBytes, fmtCountdown, fmtDuration, fmtPct, fmtUntil, level } from "../../static/js/util.js";

let failed = 0;
const eq = (actual, expected, label) => {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) { console.log(`FAIL ${label}: got ${JSON.stringify(actual)}, expected ${JSON.stringify(expected)}`); failed++; }
};

eq(niceMax(0), 1, "niceMax zero"); eq(niceMax(0.3), 0.5, "niceMax 0.3"); eq(niceMax(7), 10, "niceMax 7"); eq(niceMax(23), 25, "niceMax 23"); eq(niceMax(101), 200, "niceMax 101");
eq(niceBytesMax(0), 1024, "bytes zero"); eq(niceBytesMax(900), 1024, "900 B"); eq(niceBytesMax(3.4 * 1048576), 4 * 1048576, "3.4 MB");
eq(niceBytesMax(5 * 1048576), 8 * 1048576, "5 MB"); eq(niceBytesMax(60 * 1024), 80 * 1024, "60 KB"); eq(niceBytesMax(1.2 * 2 ** 30), 2 * 2 ** 30, "1.2 GB");

const pts = [[10, 1], [20, 2], [30, 3], [40, 4]];
eq(nearest(pts, 0), 0, "nearest before"); eq(nearest(pts, 24), 1, "nearest 24"); eq(nearest(pts, 26), 2, "nearest 26"); eq(nearest(pts, 99), 3, "nearest after"); eq(nearest([[5, 1]], 5), 0, "nearest single");

eq(fmtBytes(0), "0 B", "0 B"); eq(fmtBytes(1536), "1.50 KB", "1.5 KB"); eq(fmtBytes(1073741824), "1.00 GB", "1 GB"); eq(fmtBytes(null), "–", "null bytes"); eq(fmtBytes(NaN), "–", "NaN bytes");
eq(fmtDuration(59), "59s", "59 s"); eq(fmtDuration(125), "2m 5s", "2 m"); eq(fmtDuration(7260), "2h 1m", "2 h"); eq(fmtDuration(90061), "1d 1h", "1 d"); eq(fmtDuration(undefined), "–", "undefined duration");
eq(fmtPct(12.345), "12.3%", "pct"); eq(fmtPct(null), "–", "null pct");
eq(fmtAgo(1000, 1002), "just now", "ago now"); eq(fmtAgo(1000, 1030), "30s ago", "ago 30 s"); eq(fmtAgo(1000, 1000 + 600), "10 min ago", "ago 10 min"); eq(fmtAgo(1000, 1000 + 7200), "2 h ago", "ago 2 h"); eq(fmtAgo(1000, 1000 + 3 * 86400), "3 d ago", "ago 3 d");
eq(fmtUntil(2000, 1000), "in 17 min", "until"); eq(fmtUntil(900, 1000), "now", "until past");

const high = { warn: 85, crit: 95 }, low = { warn: 15, crit: 7 };
eq(level(high, 50), "ok", "level ok"); eq(level(high, 85), "warn", "level warn edge"); eq(level(high, 96), "crit", "level crit"); eq(level(undefined, 99), "ok", "no rule"); eq(level(high, null), "ok", "no value");
eq(level(low, 40, true), "ok", "low ok"); eq(level(low, 15, true), "warn", "low warn edge"); eq(level(low, 3, true), "crit", "low crit");

// el(): flag attributes follow truthiness, so "" / 0 / null / undefined / false never switch them on.
const made = [];
globalThis.document = {
  createElement: (tag) => { const node = { tag, attrs: {}, className: "", children: [], setAttribute(k, v) { this.attrs[k] = v; }, addEventListener() {}, append(...c) { this.children.push(...c); } }; made.push(node); return node; },
  createTextNode: (t) => ({ text: t }),
};
globalThis.Node = class {};
const { el } = await import("../../static/js/util.js");
for (const off of ["", 0, null, undefined, false]) eq("disabled" in el("button", { disabled: off }).attrs, false, `disabled=${JSON.stringify(off)} must stay off`);
for (const on of [true, "yes", 1]) eq("disabled" in el("button", { disabled: on }).attrs, true, `disabled=${JSON.stringify(on)} must switch on`);
eq("hidden" in el("p", { hidden: "" }).attrs, false, "hidden empty string");
eq(el("p", { style: "color:red" }).attrs.style, undefined, "inline style is never set");
eq(el("p", { title: "x" }).attrs.title, "x", "ordinary attribute");

eq(fmtCountdown(42), "0:42", "countdown seconds"); eq(fmtCountdown(60), "1:00", "countdown minute"); eq(fmtCountdown(725), "12:05", "countdown minutes"); eq(fmtCountdown(3723), "1:02:03", "countdown hours");
eq(fmtCountdown(0), "now", "countdown zero"); eq(fmtCountdown(-5), "now", "countdown negative"); eq(fmtCountdown(0.4), "now", "countdown rounds to zero"); eq(fmtCountdown(NaN), "now", "countdown NaN"); eq(fmtCountdown(null), "now", "countdown null");

// Idle tracker: a fake clock stands in for Date.now.
const { idleTracker, PING_MS } = await import("../../static/js/idle.js");
let clock = 1_000_000;
const tick = (seconds) => { clock += seconds * 1000; };
const tracker = idleTracker(15 * 60 * 1000, () => clock);
eq(tracker.idle(), false, "fresh tracker is not idle");
eq(tracker.pingDue(), false, "no ping without input");
tick(14 * 60); eq(tracker.idle(), false, "14 min is not idle"); eq(tracker.leftMs(), 60000, "one minute left");
tick(60); eq(tracker.idle(), true, "15 min is idle"); eq(tracker.leftMs(), 0, "nothing left");
tracker.input(); eq(tracker.idle(), true, "input after the limit does not revive the session");

const active = idleTracker(15 * 60 * 1000, () => clock);
tick(PING_MS / 1000 + 1); eq(active.pingDue(), false, "time alone never triggers a ping");
active.input(); eq(active.pingDue(), true, "input after a quiet period triggers a ping");
active.pinged(); eq(active.pingDue(), false, "not again straight after a ping");
tick(5); active.input(); eq(active.pingDue(), false, "input inside the heartbeat period waits");
tick(PING_MS / 1000); eq(active.pingDue(), true, "due once the period has passed");
active.pinged(); tick(14 * 60); eq(active.idle(), false, "input keeps resetting the clock");
tick(15 * 60); eq(active.idle(), true, "and silence ends it");
eq(active.pingDue(), false, "an idle tab never pings");

// Overview rows: what the compact "Live usage" card shows for each resource.
const { summary } = await import("../../static/js/usage.js");
const th = { cpu_pct: { warn: 85, crit: 95 }, mem_avail_pct: { warn: 15, crit: 7 }, swap_pct: { warn: 50, crit: 80 }, disk_pct: { warn: 80, crit: 90 } };
const cpuRow = summary.cpu({ total: { busy: 3.2 }, load: [0.2, 0.14, 0.12], cores: 1 }, th);
eq([cpuRow.pct, cpuRow.lvl, cpuRow.value, cpuRow.sub], [3.2, "ok", "3.2%", "load 0.20 · 1 core"], "cpu row");
eq(summary.cpu({ total: { busy: 97 }, load: [3, 2, 1], cores: 4 }, th).lvl, "crit", "cpu crit");
eq(summary.cpu({ total: null, load: [0, 0, 0], cores: 1 }, th).value, "…", "cpu still measuring");
const memRow = summary.memory({ used_pct: 74, available_pct: 26, used: 707 * 1048576, total: 956 * 1048576 }, th);
eq([memRow.lvl, memRow.value, memRow.sub], ["ok", "74.0%", "707 MB of 956 MB"], "memory row");
eq(summary.memory({ used_pct: 96, available_pct: 4, used: 1, total: 2 }, th).lvl, "crit", "memory low available is critical");
eq(summary.swap({ swap_total: 0 }, th), { value: "none", sub: "no swap configured" }, "no swap");
eq(summary.swap({ swap_total: 4 * 2 ** 30, swap_used: 2 ** 30, swap_used_pct: 25, swap_in_Bps: 0, swap_out_Bps: 0 }, th).sub, "1.00 GB of 4.00 GB", "idle swap");
eq(summary.swap({ swap_total: 4 * 2 ** 30, swap_used: 2 ** 30, swap_used_pct: 25, swap_in_Bps: 2048, swap_out_Bps: 0 }, th).sub, "1.00 GB of 4.00 GB · swapping 2.00 KB/s", "active swap");
eq(summary.disk({ error: "gone" }, th).lvl, "warn", "disk error is a warning");
eq(summary.disk({ used_pct: 91, used: 91, size: 100, available: 9 }, th).lvl, "crit", "disk nearly full");
const ioRow = summary.io({ devices: [{ busy_pct: 5, read_Bps: 1024, write_Bps: 0 }, { busy_pct: 75, read_Bps: 1024, write_Bps: 2048 }] });
eq([ioRow.pct, ioRow.lvl, ioRow.value, ioRow.sub], [75, "warn", "75% busy", "read 2.00 KB/s · write 2.00 KB/s"], "disk activity uses the busiest disk");
eq(summary.io({ devices: [] }).value, "–", "no disks");
const netRow = summary.net({ interfaces: [{ up: true, rx_Bps: 1024, tx_Bps: 512 }, { up: true, rx_Bps: 1024, tx_Bps: 512 }] });
eq([netRow.pct, netRow.lvl, netRow.value, netRow.sub], [undefined, "ok", "in 2.00 KB/s", "out 1.00 KB/s · 2 of 2 up"], "network sums the interfaces and has no bar");
eq(summary.net({ interfaces: [{ up: true, rx_Bps: 0, tx_Bps: 0 }, { up: false, rx_Bps: 0, tx_Bps: 0 }] }).lvl, "warn", "an interface that is down");
const psiRow = summary.pressure([["CPU", { some: { avg10: 1 } }], ["memory", { some: { avg10: 30 } }], ["disk", { some: { avg10: 2 } }]]);
eq([psiRow.pct, psiRow.lvl, psiRow.sub], [30, "warn", "mostly waiting for memory"], "pressure names the worst resource");
eq(summary.pressure([["CPU", { some: { avg10: 0 } }], ["memory", null]]).sub, "nothing is waiting", "no pressure");
eq(summary.pressure([["CPU", null]]).value, "–", "pressure unavailable");
eq(summary.host({ uptime_s: 61200, os: "Ubuntu" }), { value: "up 17h 0m", sub: "Ubuntu" }, "host row");

// Card ordering (drag and drop): the rules behind the grips.
const { applyOrder, moveKey, neighbourKey } = await import("../../static/js/dragsort.js");
const natural = ["live", "cpu", "memory", "swap", "io", "net"];
eq(applyOrder(natural, []), natural, "no saved order keeps the natural one");
eq(applyOrder(natural, ["net", "cpu", "live"]), ["net", "cpu", "memory", "swap", "io", "live"], "keys missing from the saved order follow their predecessor");
eq(applyOrder(["a", "b", "c"], ["c", "gone", "a", "c"]), ["c", "a", "b"], "stale and duplicate saved keys are ignored; a new key follows its predecessor");
eq(applyOrder(["a", "x", "b"], ["b", "a"]), ["b", "a", "x"], "a new key goes after the key that preceded it, wherever that moved");
eq(applyOrder(["x", "a"], ["a"]), ["x", "a"], "a new first key stays first");
eq(moveKey(["a", "b", "c", "d"], "a", "c", true), ["b", "c", "a", "d"], "move after");
eq(moveKey(["a", "b", "c", "d"], "d", "b", false), ["a", "d", "b", "c"], "move before");
eq(moveKey(["a", "b"], "a", "a", true), ["a", "b"], "moving onto itself changes nothing");
eq(moveKey(["a", "b"], "a", "zz", true), ["a", "b"], "unknown target changes nothing");
const shown = new Set(["a", "c", "d"]);
eq(neighbourKey(["a", "b", "c", "d"], shown, "a", 1), "c", "the next visible card skips hidden ones");
eq(neighbourKey(["a", "b", "c", "d"], shown, "d", -1), "c", "previous visible");
eq(neighbourKey(["a", "b", "c", "d"], shown, "a", -1), null, "nothing before the first");
eq(neighbourKey(["a", "b", "c", "d"], shown, "d", 1), null, "nothing after the last");

if (failed) { console.log(`${failed} check(s) failed`); process.exit(1); }
console.log("front-end helpers ok");
