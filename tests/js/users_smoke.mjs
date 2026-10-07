// The Users tab against the fake DOM: what shows by default, filtering, the details dialog, the two session tables.

import { installFakeDom } from "./fakedom.mjs";

let failed = 0;
const check = (ok, label) => { if (!ok) { console.log(`FAIL ${label}`); failed++; } };
installFakeDom();

const { default: users } = await import("../../static/js/tabs/users.js");

const base = { comment: "", groups: [], primary_group: "x", sudo: null, sudo_via: [], nopasswd: false, password: "set", password_expired: false, must_change: false,
  expired: false, expires: null, password_expires: null, password_changed: null, keys: 0, keys_error: null, can_login: true, blockers: [], home: "/home/x",
  home_exists: true, home_owner: "x", home_owner_ok: true, home_mode: "0750", shell: "/bin/bash", processes: 0, last_login: null };
const account = (over) => ({ ...base, ...over });
const USERS = [
  account({ name: "root", uid: 0, type: "root", sudo: "full", sudo_via: ["sudoers"], home: "/root", keys: 1, last_login: { time: 1_700_000_000, from: "10.0.0.5", tty: "pts/1" } }),
  account({ name: "alice", uid: 1000, type: "login", comment: "Alice A", groups: ["alice", "sudo"], sudo: "full", sudo_via: ["sudoers"], keys: 2 }),
  account({ name: "bob", uid: 1001, type: "login", password: "locked", keys: 0 }),
  account({ name: "carol", uid: 1002, type: "login", shell: "/usr/sbin/nologin", can_login: false, blockers: ["its shell does not allow logins"], keys: null }),
  account({ name: "daemon", uid: 1, type: "system", shell: "/usr/sbin/nologin", password: "none", keys: null, can_login: false, home_exists: false }),
  account({ name: "www-data", uid: 33, type: "system", shell: "/usr/sbin/nologin", password: "none", keys: null, can_login: false, home_exists: false }),
];
const SESSIONS = [
  { id: "7", user: "root", uid: 0, service: "sshd", tty: "pts/1", from: "10.0.0.5", state: "active", idle: false, idle_since: null, since: 1_700_000_000 - 600, processes: 3 },
  { id: "9", user: "alice", uid: 1000, service: "sshd", tty: "", from: "10.0.0.6", state: "closing", idle: true, idle_since: 1_700_000_000 - 120, since: 1_700_000_000 - 3000, processes: 5 },
];
const DASH = [
  { id: "aaaaaaaaaaaa", ip: "100.1.1.1", ua: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36", created: 1, last_seen: 5, idle_left: 800, expires: 99, current: true },
  { id: "bbbbbbbbbbbb", ip: "100.2.2.2", ua: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1", created: 1, last_seen: 4, idle_left: 100, expires: 99, current: false },
];
const live = (data, extra = {}) => ({ sections: { users: data ? { t: 1, data } : null }, dashboard_sessions: DASH, ...extra });
const full = { users: USERS, sessions: SESSIONS, notes: {}, sudo_source: "sudoers" };

function make() {
  const dialogs = [];
  const ctx = { openDialog: (title, content) => dialogs.push([title, content]) };
  const tab = users.create(ctx);
  return { tab, dialogs };
}
const panels = (tab) => tab.el.children;                     // [signed in now, users]
const rows = (panel) => panel.find((n) => n.tag === "tr" && n.hasClass("clickable"));
const accountRows = (tab) => rows(panels(tab)[1]);
const names = (tab) => accountRows(tab).map((r) => r.children[0].textContent.match(/^[a-z-]+/)[0]);
const toolbar = (tab) => panels(tab)[1].find((n) => n.hasClass("toolbar"))[0];
const input = (tab, type) => toolbar(tab).find((n) => n.tag === "input" && n.attrs.type === type)[0];

{
  const { tab } = make();
  tab.update(live(full));
  check(names(tab).join() === "root,alice,bob,carol", `system accounts are hidden at first: ${names(tab)}`);
  check(panels(tab)[1].textContent.includes("4 of 6 accounts"), "the count says how many are hidden");

  const toggle = input(tab, "checkbox");
  toggle.checked = true; toggle.fire("change");
  check(names(tab).join() === "root,alice,bob,carol,daemon,www-data", `people first, then system accounts: ${names(tab)}`);
  toggle.checked = false; toggle.fire("change");
  check(names(tab).length === 4, "and hide them again");

  const search = input(tab, "search");
  search.value = "ali"; search.fire("input");
  check(names(tab).join() === "alice", `filter by name: ${names(tab)}`);
  search.value = "sudo"; search.fire("input");
  check(names(tab).join() === "alice", `filter by group: ${names(tab)}`);
  search.value = "alice a"; search.fire("input");
  check(names(tab).join() === "alice", "filter by full name");
  search.value = "nobody-here"; search.fire("input");
  check(accountRows(tab).length === 0 && panels(tab)[1].textContent.includes("Nothing to show"), "no match says so");
  search.value = ""; search.fire("input");
  check(names(tab).length === 4, "clearing the filter brings everyone back");
  toggle.checked = true; toggle.fire("change");
  search.value = "daem"; search.fire("input");
  check(names(tab).join() === "daemon", "a system account is found once system accounts are shown");
}

// badges and the dialog
{
  const { tab, dialogs } = make();
  tab.update(live(full));
  const byName = (n) => accountRows(tab).find((r) => r.children[0].textContent.startsWith(n));
  check(byName("alice").textContent.includes("sudo") && byName("alice").textContent.includes("2 keys"), `alice's badges: ${byName("alice").textContent}`);
  check(byName("bob").textContent.includes("locked") && byName("bob").textContent.includes("no keys"), "bob is locked and has no keys");
  check(byName("carol").textContent.includes("cannot log in"), "carol cannot log in");
  check(byName("root").textContent.includes("10.0.0.5"), "last login shows where from");
  check(byName("bob").textContent.includes("never"), "bob never logged in");

  byName("alice").click();
  check(dialogs.length === 1 && dialogs[0][0] === "alice", "clicking an account opens its dialog");
  const text = dialogs[0][1].textContent;
  check(text.includes("yes, all commands (sudoers)") && text.includes("Signed in now") && text.includes("#9 from 10.0.0.6"), `the dialog has the details and her session: ${text.slice(0, 200)}`);
  check(!text.includes("#7"), "and only her own session");
  check(!/\bundefined\b|\bnull\b/.test(text), "no 'undefined' or 'null' text");
}

// the two session tables
{
  const { tab } = make();
  tab.update(live(full));
  const tables = panels(tab)[0].find((n) => n.tag === "tbody");
  const text = panels(tab)[0].textContent;
  check(text.includes("SSH · pts/1") && text.includes("10.0.0.5") && text.includes("closing") && text.includes("idle"), `system sessions: ${text.slice(0, 300)}`);
  check(text.includes("Chrome on macOS") && text.includes("Safari on iOS") && text.includes("you"), "dashboard sign-ins with browser names and 'you'");
  check(text.includes("100.1.1.1") && text.includes("100.2.2.2"), "and their addresses");
  check(tables.length === 2 && tables.every((t) => t.children.length === 2), `two rows in each table: ${tables.map((t) => t.children.length)}`);
}

// notes, missing and broken data
{
  const { tab } = make();
  tab.update(live({ ...full, notes: { shadow: "password and expiry details need root", sudo: "sudoers is not readable" } }));
  const note = panels(tab)[1].find((n) => n.hasClass("err"))[0];
  check(note && !note.hidden && note.textContent.includes("need root") && note.textContent.includes("sudoers is not readable"), "notes about partial data are shown");
  tab.update(live(full));
  check(note.hidden, "and hidden again when there are none");
}
{
  const { tab } = make();
  tab.update(live(null));
  check(panels(tab)[1].textContent.includes("Waiting for data"), "before the first collection it says so");
  check(panels(tab)[0].textContent.includes("Chrome on macOS"), "the dashboard sign-ins still show");
  tab.update(live({ error: "boom" }));
  check(panels(tab)[1].textContent.includes("Unavailable: boom"), "a collector error is shown");
  tab.update({ sections: { users: { t: 1, data: full } } });
  check(panels(tab)[0].textContent.includes("No dashboard sign-ins"), "no sign-in list at all is an empty list, not a crash");
}

if (failed) { console.log(`${failed} check(s) failed`); process.exit(1); }
console.log("users smoke ok");
