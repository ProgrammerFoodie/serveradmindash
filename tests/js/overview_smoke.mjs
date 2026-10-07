// Renders the Overview tab against a tiny fake DOM and clicks its rows: nothing here needs a browser,
// but it catches typos and wrong assumptions in the DOM code that the pure-function tests cannot.

let failed = 0;
const check = (ok, label) => { if (!ok) { console.log(`FAIL ${label}`); failed++; } };

import { installFakeDom } from "./fakedom.mjs";

const { saved } = installFakeDom();

const { default: overview } = await import("../../static/js/tabs/overview.js");

const MB = 1048576;
const live = { sections: {
  system: { data: { hostname: "h", os: "Ubuntu", kernel: "7.0", arch: "x86_64", uptime_s: 61200, cpu_count: 1, cpu_model: "CPU", virtualization: "kvm", timezone: "UTC", time_sync: { synced: true, offset_s: 0.0002 } } },
  cpu: { data: { total: { busy: 3.2, user: 2, nice: 0, system: 1, irq: 0, softirq: 0, iowait: 0, steal: 0 }, load: [0.2, 0.14, 0.12], load_per_core: 0.2, cores: 1,
                 tasks_running: 1, tasks_blocked: 0, tasks_total: 288, ctxt_per_s: 832, forks_per_s: 0, per_core: [{ busy: 3.2 }], pressure: { some: { avg10: 0, avg60: 0, avg300: 0 } } } },
  memory: { data: { used_pct: 74, available_pct: 26, used: 707 * MB, total: 956 * MB, available: 249 * MB, cached: 280 * MB, buffers: 5 * MB, slab_reclaimable: 50 * MB, slab_unreclaimable: 54 * MB,
                    anon: 438 * MB, shmem: 4 * MB, dirty: 1, writeback: 0, committed: 3 * 2 ** 30, commit_limit: 5 * 2 ** 30, swap_used_pct: 32.4, swap_used: 2 ** 30, swap_total: 4 * 2 ** 30,
                    swap_in_Bps: 19000, swap_out_Bps: 0, major_faults_per_s: 5.6, oom_kills_total: 0, swaps: [{ name: "/swapfile", type: "file", used: 2 ** 30, size: 4 * 2 ** 30, used_pct: 25 }],
                    pressure: { some: { avg10: 0.5, avg60: 0, avg300: 0 }, full: { avg10: 0, avg60: 0, avg300: 0 } } } },
  disk_io: { data: { devices: [{ device: "sda", role: "/", busy_pct: 1, read_Bps: 155000, write_Bps: 5600, read_iops: 16, write_iops: 0, await_ms: 0.3 }],
                     pressure: { some: { avg10: 1, avg60: 0, avg300: 0 }, full: { avg10: 0, avg60: 0, avg300: 0 } } } },
  net_io: { data: { interfaces: [{ name: "eth0", up: true, rx_Bps: 4000, tx_Bps: 3500, rx_pps: 16, tx_pps: 11, rx_errors: 0, tx_errors: 0, rx_drops: 0, tx_drops: 0 }] } },
  disks: { data: { mounts: [
    { mount: "/", device: "/dev/sda", fstype: "ext4", used_pct: 60.3, used: 13.7 * 2 ** 30, size: 24 * 2 ** 30, available: 9 * 2 ** 30, inodes_pct: 14.6, inodes_total: 1593088, readonly: false },
    { mount: "/mnt/Extra20", device: "/dev/sdc", fstype: "ext4", used_pct: 20.7, used: 3.8 * 2 ** 30, size: 19.5 * 2 ** 30, available: 14.7 * 2 ** 30, inodes_pct: 9.4, inodes_total: 1310720, readonly: false }] } },
} };

const calls = [];
const ctx = { thresholds: { cpu_pct: { warn: 85, crit: 95 }, mem_avail_pct: { warn: 15, crit: 7 }, swap_pct: { warn: 50, crit: 80 }, disk_pct: { warn: 80, crit: 90 }, inode_pct: { warn: 80, crit: 90 } },
  range: "1h", history: async (metrics) => { calls.push(metrics); return { series: {} }; }, rangePicker: () => document.createElement("div") };

const settle = () => new Promise((resolve) => setTimeout(resolve, 5));
const heads = (tab) => tab.el.find((n) => n.hasClass("usage-head"));
const names = (tab) => heads(tab).map((h) => h.children[0].textContent);
const cardOf = (tab, head) => tab.el.find((n) => n.id && n.id === head.getAttribute("aria-controls"))[0];
const board = (tab) => tab.el.children[1];          // children: the Live usage panel, then the board of cards
const boardKeys = (tab) => board(tab).children.map((c) => c.getAttribute("data-key"));
const button = (node, label) => node.find((n) => n.tag === "button" && n.textContent === label)[0];
const rowText = (tab, name) => heads(tab)[names(tab).indexOf(name)].children.filter((c) => c.tag === "span").map((c) => c.textContent).join(" · ");

const tab = overview.create(ctx);
tab.update(live);
await settle();

check(names(tab).join("|") === "CPU|Memory|Swap|Disk /|Disk /mnt/Extra20|Disk activity|Network|Pressure|Host", `row order: ${names(tab)}`);
check(boardKeys(tab).join("|") === "cpu|memory|swap|disk:/|disk:/mnt/Extra20|io|net|psi|host", `card order: ${boardKeys(tab)}`);

// a first visit shows CPU and Memory; everything else is closed
const openNames = () => heads(tab).filter((h) => h.getAttribute("aria-expanded") === "true").map((h) => h.children[0].textContent);
check(openNames().join("|") === "CPU|Memory", `first visit opens CPU and Memory: ${openNames()}`);
check(heads(tab).every((h) => cardOf(tab, h).hidden === (h.getAttribute("aria-expanded") !== "true")), "a card is visible exactly when its row is open");

// rows carry the live numbers
check(rowText(tab, "CPU").includes("3.2%") && rowText(tab, "CPU").includes("load 0.20"), `cpu row: ${rowText(tab, "CPU")}`);
check(rowText(tab, "Memory").includes("74.0%") && rowText(tab, "Memory").includes("707 MB of 956 MB"), `memory row: ${rowText(tab, "Memory")}`);
check(rowText(tab, "Swap").includes("32.4%"), `swap row: ${rowText(tab, "Swap")}`);
check(rowText(tab, "Disk /mnt/Extra20").includes("20.7%"), `disk row: ${rowText(tab, "Disk /mnt/Extra20")}`);
check(rowText(tab, "Network").includes("in 3.91 KB/s"), `network row: ${rowText(tab, "Network")}`);
check(rowText(tab, "Host").includes("up 17h 0m"), `host row: ${rowText(tab, "Host")}`);

// graphs belong to their card: only open cards load history
const asked = new Set(calls.flat());
check(asked.has("cpu.busy") && asked.has("load.1") && asked.has("mem.used_pct"), "open cards load their graphs");
check(!asked.has("psi.cpu_some") && !asked.has("net.eth0.rx_Bps") && !asked.has("fs.//mnt/Extra20.used_pct"), `closed cards load nothing: ${[...asked]}`);
check(cardOf(tab, heads(tab)[0]).find((n) => n.tag === "canvas").length === 2, "the CPU card holds its two graphs");
check(cardOf(tab, heads(tab)[0]).textContent.includes("Load per core"), "and its details");

// opening a row opens its card, loads its graphs and remembers it
const psiHead = heads(tab)[names(tab).indexOf("Pressure")];
calls.length = 0;
psiHead.click(); await settle();
check(psiHead.getAttribute("aria-expanded") === "true" && !cardOf(tab, psiHead).hidden, "clicking a row opens its card");
check(calls.flat().includes("psi.cpu_some"), "and loads its graph");
check(JSON.parse(saved.get("overview_open")).sort().join() === "cpu,memory,psi", `open state saved: ${saved.get("overview_open")}`);
button(cardOf(tab, psiHead), "Hide").click(); await settle();
check(psiHead.getAttribute("aria-expanded") === "false" && cardOf(tab, psiHead).hidden, "the card's Hide button closes it");
calls.length = 0;
await tab.onRange(); await settle();
check(!calls.flat().includes("psi.cpu_some") && calls.flat().includes("cpu.busy"), "a closed card stops loading history");

// graphs that need names from the first live data show up in their cards
const ioHead = heads(tab)[names(tab).indexOf("Disk activity")];
ioHead.click(); await settle();
check(cardOf(tab, ioHead).find((n) => n.tag === "canvas").length === 2, "the disk card got its busy and throughput graphs");
check(calls.flat().includes("io.sda.busy_pct"), "and they load once it is open");

// open all / close all
const all = button(tab.el, "Open all");
all.click();
check(heads(tab).every((h) => h.getAttribute("aria-expanded") === "true") && all.textContent === "Close all", "open all opens every card");
all.click();
check(heads(tab).every((h) => h.getAttribute("aria-expanded") === "false") && all.textContent === "Open all", "close all closes them");

// a fresh page load restores what was open, including a disk
heads(tab)[4].click(); heads(tab)[1].click();
const again = overview.create(ctx);
again.update(live);
const restored = heads(again).filter((h) => h.getAttribute("aria-expanded") === "true").map((h) => h.children[0].textContent);
check(restored.join("|") === "Memory|Disk /mnt/Extra20", `restored after reload: ${restored}`);

// a saved card order is applied, new disk cards appear just before Disk activity, and Reset puts it back
saved.set("overview_order", JSON.stringify(["host", "psi", "net", "io", "swap", "memory", "cpu"]));
const custom = overview.create(ctx);
check(boardKeys(custom).join("|") === "host|psi|net|io|swap|memory|cpu", `saved order applied: ${boardKeys(custom)}`);
custom.update(live);
check(boardKeys(custom).join("|") === "host|psi|net|disk:/|disk:/mnt/Extra20|io|swap|memory|cpu", `new disks sit before Disk activity: ${boardKeys(custom)}`);
button(custom.el, "Reset order").click();
check(boardKeys(custom).join("|") === "cpu|memory|swap|disk:/|disk:/mnt/Extra20|io|net|psi|host", `reset restores the natural order: ${boardKeys(custom)}`);
check(JSON.parse(saved.get("overview_order")).length === 0, "and forgets the saved one");

// a corrupt saved state falls back to the defaults instead of breaking the page
saved.set("overview_open", "{not json"); saved.set("overview_order", "42");
const broken = overview.create(ctx);
broken.update(live);
check(boardKeys(broken)[0] === "cpu" && heads(broken).filter((h) => h.getAttribute("aria-expanded") === "true").length === 2, "corrupt saved state falls back to defaults");

// data that is missing or broken never throws, and says so
const odd = overview.create({ ...ctx });
odd.update({ sections: { cpu: null, memory: { data: { error: "boom" } }, disks: { data: { mounts: [{ mount: "/x", error: "gone" }] } } } });
const oddText = odd.el.find((n) => n.hasClass("usage-sub")).map((n) => n.textContent).join("|");
check(oddText.includes("waiting for data") && oddText.includes("boom") && oddText.includes("unavailable: gone"), `degraded rows: ${oddText}`);

if (failed) { console.log(`${failed} check(s) failed`); process.exit(1); }
console.log("overview smoke ok");
