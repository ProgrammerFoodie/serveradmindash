// Clickable table rows must work from the keyboard and keep focus across the 5-second refresh.
let failed = 0;
const check = (ok, label) => { if (!ok) { console.log(`FAIL ${label}`); failed++; } };

import { installFakeDom } from "./fakedom.mjs";
installFakeDom();
const { dataTable } = await import("../../static/js/util.js");

const opened = [];
const table = dataTable({ columns: [{ key: "name", label: "Name" }], sortKey: "name", sortDir: 1, onRowClick: (r) => opened.push(r.name) });
table.setRows([{ name: "a" }, { name: "b" }]);
const rows = () => table.el.find((n) => n.tag === "tr" && n.hasClass("clickable"));

check(rows().length === 2 && rows().every((r) => r.getAttribute("tabindex") === "0"), "every clickable row can take focus");
rows()[1].fire("keydown", { key: "Enter" });
rows()[0].fire("keydown", { key: " " });
rows()[0].fire("keydown", { key: "a" });
check(opened.join() === "b,a", `Enter and Space open the row, other keys do not: ${opened}`);

// focus stays on the same row position when the table is repainted
rows()[0].constructor.prototype.focus = function focus() { this.gotFocus = true; };
globalThis.document.activeElement = rows()[1];
table.setRows([{ name: "a" }, { name: "b" }]);
check(rows()[1].gotFocus === true && !rows()[0].gotFocus, "keyboard focus is put back on the same row after a refresh");
globalThis.document.activeElement = null;
table.setRows([{ name: "a" }, { name: "b" }]);
check(!rows()[1].gotFocus || rows()[1].gotFocus === undefined, "nothing is focused when nothing was");

const plain = dataTable({ columns: [{ key: "name", label: "Name" }], sortKey: "name" });
plain.setRows([{ name: "x" }]);
check(plain.el.find((n) => n.tag === "tr" && n.getAttribute("tabindex") !== null).length === 0, "rows without an action are not focus stops");

if (failed) process.exit(1);
console.log("ok");
