// The folder picker against the fake DOM and a fake /api/folders.

import { FakeNode, installFakeDom } from "./fakedom.mjs";

let failed = 0;
const check = (ok, label) => { if (!ok) { console.log(`FAIL ${label}`); failed++; } };
installFakeDom();

const { crumbs, nameProblem, pickFolder, previewPath } = await import("../../static/js/folderpicker.js");
const settle = (ms = 30) => new Promise((resolve) => setTimeout(resolve, ms));

// ---- pure helpers
check(JSON.stringify(crumbs("/mnt/Extra20")) === JSON.stringify([{ label: "/", path: "/" }, { label: "mnt", path: "/mnt" }, { label: "Extra20", path: "/mnt/Extra20" }]), "breadcrumbs");
check(crumbs("/").length === 1 && crumbs("").length === 1 && crumbs(null).length === 1, "breadcrumbs of the top, and of nothing");
check(previewPath("/mnt/Extra20", "bob") === "/mnt/Extra20/bob" && previewPath("/", "bob") === "/bob" && previewPath("/mnt", "") === "/mnt", "preview paths");
for (const bad of ["a/b", "..", ".", " lead", "trail ", "x".repeat(65), "a\nb", "a\u0000b"]) check(nameProblem(bad) !== null, `a bad name: ${JSON.stringify(bad)}`);
for (const good of ["", "bob", "my folder", "x".repeat(64), "ünï"]) check(nameProblem(good) === null, `a good name: ${JSON.stringify(good)}`);

// ---- a tiny pretend server
const PLACES = [{ path: "/home", device: "/dev/sda", free: 9 * 2 ** 30, total: 24 * 2 ** 30, writable: true },
  { path: "/mnt/Extra20", device: "/dev/sdc", free: 14.7 * 2 ** 30, total: 19.5 * 2 ** 30, writable: true },
  { path: "/mnt/ro", device: "/dev/sdd", free: 1, total: 2, writable: false }];
const FOLDERS = {
  "/mnt/Extra20": [{ name: "admin", path: "/mnt/Extra20/admin", kind: "folder", enterable: true, selectable: false, reason: "that overlaps this dashboard's own folder" },
    { name: "projects", path: "/mnt/Extra20/projects", kind: "folder", enterable: true, selectable: true, reason: "" },
    { name: "alice", path: "/mnt/Extra20/alice", kind: "folder", enterable: false, selectable: false, reason: "the home folder of alice" },
    { name: "link", path: "/mnt/Extra20/link", kind: "link", enterable: false, selectable: false, reason: "a symbolic link" }],
  "/mnt/Extra20/projects": [], "/mnt": [{ name: "Extra20", path: "/mnt/Extra20", kind: "folder", enterable: true, selectable: true, reason: "" }], "/home": [], "/mnt/ro": [],
};
function server({ selectable = {}, badChoice = false } = {}) {
  const log = [];
  return { log, api: { async get(u) {
    log.push(u);
    const q = new URL(`http://x${u}`);
    const path = q.searchParams.get("path") || "/home";
    if (!(path in FOLDERS)) throw new Error(`${path} is not a folder`);
    const listing = { path, parent: path === "/" ? null : path.replace(/\/[^/]*$/, "") || "/", places: PLACES, folders: FOLDERS[path], truncated: false,
      writable: path !== "/mnt/ro", selectable: selectable[path] ?? path !== "/home", reason: (selectable[path] ?? path !== "/home") ? "" : "/home itself cannot be a home folder" };
    if (q.searchParams.has("name")) {
      const name = q.searchParams.get("name");
      listing.choice = badChoice ? { path: null, ok: false, reason: "that overlaps the home folder of alice", exists: false } : { path: `${path}/${name}`, ok: true, reason: "", exists: false };
    }
    if (q.searchParams.get("hidden") === "1") listing.folders = [{ name: ".cache", path: `${path}/.cache`, kind: "folder", enterable: true, selectable: true, reason: "" }, ...listing.folders];
    return listing;
  } } };
}
function makeDialog() {
  const dialog = new FakeNode("dialog");
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = (value) => { if (value !== undefined) dialog.returnValue = value; dialog.open = false; dialog.fire("close"); };
  return dialog;
}
const rows = (d) => d.find((n) => n.hasClass("folder-row"));
const row = (d, name) => rows(d).find((r) => r.textContent.includes(name));
const button = (d, label) => d.find((n) => n.tag === "button" && n.textContent === label)[0];
const nameBox = (d) => d.find((n) => n.attrs && n.attrs.name === "folder-name")[0];
const preview = (d) => d.find((n) => n.hasClass("folder-preview"))[0].textContent;
const errorText = (d) => { const e = d.find((n) => n.hasClass("form-error"))[0]; return e && !e.hidden ? e.textContent : ""; };
const choose = (d) => button(d, "Choose this folder");

// ---- opening, places, path, rows
{
  const { api, log } = server();
  const dialog = makeDialog();
  const answer = pickFolder(dialog, api, { start: "/mnt/Extra20", name: "bob", forUser: "bob" });
  await settle();
  check(dialog.open && log[0] === "/api/folders?path=%2Fmnt%2FExtra20&hidden=0&for=bob", `it asks for the starting folder: ${log[0]}`);
  const text = dialog.textContent;
  check(text.includes("/home") && text.includes("/mnt/Extra20") && text.includes("9.00 GB free") && text.includes("14.7 GB free"), "places with free space");
  check(dialog.find((n) => n.hasClass("place") && n.hasClass("current"))[0].textContent.includes("/mnt/Extra20"), "the current place is marked");
  check(text.includes("read-only"), "a disk the dashboard cannot write to says so");
  check(dialog.find((n) => n.hasClass("crumb")).map((b) => b.textContent).join() === "/,mnt,Extra20" && button(dialog, "Extra20").disabled, "the path is clickable except where you are");
  check(row(dialog, "projects").tag === "button" && row(dialog, "alice").tag === "div" && row(dialog, "alice").hasClass("disabled"), "folders that can be opened are buttons, the others are greyed");
  check(row(dialog, "alice").textContent.includes("the home folder of alice") && row(dialog, "link").textContent.includes("a symbolic link"), "and say why");
  check(row(dialog, "admin").tag === "button" && row(dialog, "admin").textContent.includes("overlaps this dashboard"), "a folder that can be opened but not chosen says why");
  check(preview(dialog).includes("/mnt/Extra20/bob") && nameBox(dialog).value === "bob" && !choose(dialog).disabled, "the name box is prefilled and the preview shows the result");

  // moving around
  row(dialog, "projects").click(); await settle();
  check(log[1].startsWith("/api/folders?path=%2Fmnt%2FExtra20%2Fprojects"), "clicking a folder opens it");
  check(preview(dialog).includes("/mnt/Extra20/projects/bob") && dialog.textContent.includes("No folders here"), "the preview follows, and an empty folder says so");
  button(dialog, "mnt").click(); await settle();
  check(log[2].includes("path=%2Fmnt&"), "clicking a part of the path goes there");
  dialog.find((n) => n.hasClass("place"))[0].click(); await settle();
  check(log[3].includes("path=%2Fhome&"), "clicking a place goes there");
  check(!choose(dialog).disabled, "with a name typed, choosing inside /home is fine");

  // the name box
  nameBox(dialog).value = "a/b"; nameBox(dialog).fire("input");
  check(choose(dialog).disabled && dialog.textContent.includes("cannot contain /"), "a bad name disables Choose and says why");
  nameBox(dialog).value = ""; nameBox(dialog).fire("input");
  check(choose(dialog).disabled && dialog.textContent.includes("/home itself cannot be a home folder"), "no name in a folder that cannot be a home: Choose is off and the reason shows");
  button(dialog, "Cancel").click();
  check(await answer === null && dialog.children.length === 0, "cancel resolves null and clears the dialog");
}

// ---- choosing
{
  const { api, log } = server();
  const dialog = makeDialog();
  const answer = pickFolder(dialog, api, { start: "/mnt/Extra20", name: "bob" });
  await settle();
  choose(dialog).click(); await settle();
  check(log[1].includes("name=bob") && await answer === "/mnt/Extra20/bob", `a named folder is checked by the server, then chosen: ${log[1]}`);
}
{
  const { api, log } = server();
  const dialog = makeDialog();
  const answer = pickFolder(dialog, api, { start: "/mnt/Extra20", name: "" });
  await settle();
  const before = log.length;
  choose(dialog).click(); await settle();
  check(log.length === before && await answer === "/mnt/Extra20", "no name: the folder itself, without another request");
}
{
  const { api } = server({ badChoice: true });
  const dialog = makeDialog();
  pickFolder(dialog, api, { start: "/mnt/Extra20", name: "alice" });
  await settle();
  choose(dialog).click(); await settle();
  check(dialog.open && errorText(dialog).includes("overlaps the home folder of alice"), "a refused choice keeps the dialog open and says why");
  nameBox(dialog).value = "other"; nameBox(dialog).fire("input");
  check(errorText(dialog) === "", "and the message goes away when the name changes");
}
{
  const { api } = server();
  const dialog = makeDialog();
  const answer = pickFolder(dialog, api, { start: "/mnt/Extra20", name: "x" });
  await settle();
  dialog.returnValue = ""; dialog.fire("close");
  check(await answer === null, "Escape resolves null");
}

// ---- odd situations
{
  const dialog = makeDialog();
  pickFolder(dialog, { async get() { throw new Error("the server is not answering"); } }, { start: "/mnt/Extra20", name: "bob" });
  await settle(60);
  check(errorText(dialog) === "the server is not answering", `a failure is shown, not swallowed: ${JSON.stringify(errorText(dialog))}`);
  check(choose(dialog).disabled && dialog.open, "and nothing can be chosen meanwhile");
}
{
  const { api, log } = server();
  const dialog = makeDialog();
  pickFolder(dialog, api, { start: "/no/such/place", name: "bob" });
  await settle(60);
  check(log.length === 2 && log[1].includes("path=&") && dialog.textContent.includes("/mnt/Extra20"), `a start folder that does not exist falls back to the first place: ${log}`);
}
{
  const { api, log } = server();
  const dialog = makeDialog();
  pickFolder(dialog, api, { start: "/mnt/Extra20", name: "bob" });
  await settle();
  const hide = dialog.find((n) => n.tag === "input" && n.attrs.type === "checkbox")[0];
  hide.checked = true; hide.fire("change"); await settle();
  check(log[1].includes("hidden=1") && rows(dialog)[0].textContent.includes(".cache"), "show hidden asks again and lists them");
}
{
  const { api } = server();
  const dialog = makeDialog();
  pickFolder(dialog, api, { start: "/mnt/ro", name: "bob" });
  await settle();
  check(dialog.textContent.includes("cannot write in this folder"), "a folder the dashboard cannot write in is flagged right there");
}
{
  const { api } = server();
  const dialog = makeDialog();
  pickFolder(dialog, api, { start: "/mnt/Extra20", name: "projects" });
  await settle();
  check(dialog.textContent.includes("already here"), "a name that exists is mentioned (the action decides whether that is fine)");
}

if (failed) { console.log(`${failed} check(s) failed`); process.exit(1); }
console.log("folder picker smoke ok");
