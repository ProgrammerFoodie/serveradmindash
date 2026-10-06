// Checks the pure helper functions of the front end with plain Node (no browser, no packages).
import { niceBytesMax, niceMax, nearest } from "../../static/js/chart.js";
import { fmtAgo, fmtBytes, fmtDuration, fmtPct, fmtUntil, level } from "../../static/js/util.js";

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

if (failed) { console.log(`${failed} check(s) failed`); process.exit(1); }
console.log("front-end helpers ok");
