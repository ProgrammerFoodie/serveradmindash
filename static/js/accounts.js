// What the Users tab says about an account, as plain data (strings and small objects) so it can be tested without a browser.

import { fmtAgo, fmtDateTime, fmtDuration } from "./util.js";

const TYPES = { root: "root", login: "login user", system: "system account" };
const PASSWORD = {
  set: "set",
  locked: "locked: a password exists but is switched off",
  none: "none: no password can be used to log in",
  empty: "EMPTY: anyone can log in without a password",
  unknown: "unknown (needs the dashboard to run as root)",
};

/** "Chrome on macOS" from a User-Agent header. Order matters: Edge and Chrome both say Safari, iPhones say Mac OS X. */
export function browserLabel(ua) {
  if (typeof ua !== "string" || !ua.trim()) return "unknown browser";
  const browser = /Edg(e|A|iOS)?\//.test(ua) ? "Edge" : /OPR\/|Opera/.test(ua) ? "Opera" : /Firefox\/|FxiOS/.test(ua) ? "Firefox"
    : /Chrome\/|CriOS/.test(ua) ? "Chrome" : /Version\/.*Safari\//.test(ua) ? "Safari" : null;
  const os = /iPhone|iPad|iPod/.test(ua) ? "iOS" : /Android/.test(ua) ? "Android" : /Windows/.test(ua) ? "Windows"
    : /Macintosh|Mac OS X/.test(ua) ? "macOS" : /CrOS/.test(ua) ? "ChromeOS" : /Linux|X11/.test(ua) ? "Linux" : null;
  if (browser) return os ? `${browser} on ${os}` : browser;
  return ua.split(/[\s/]/)[0].slice(0, 30) || "unknown browser";             // curl, python-requests ...
}

/** Small labels for the Users table: [{text, kind}] where kind is "", "ok", "info", "warn" or "crit". */
export function accountBadges(u) {
  const out = [];
  const person = u.type !== "system";
  if (u.sudo === "full") out.push({ text: "sudo", kind: "info" });
  else if (u.sudo === "limited") out.push({ text: "sudo (limited)", kind: "info" });
  if (u.sudo && u.nopasswd) out.push({ text: "sudo without password", kind: "warn" });
  if (u.expired) out.push({ text: "expired", kind: "crit" });
  if (u.password === "empty") out.push({ text: "empty password", kind: "crit" });
  else if (u.password === "locked") out.push({ text: "locked", kind: "warn" });
  else if (u.password === "none" && person) out.push({ text: "no password", kind: "" });
  if (u.password_expired) out.push({ text: "password expired", kind: "warn" });
  if (u.must_change) out.push({ text: "must change password", kind: "warn" });
  if (person) {
    if (u.keys === 0) out.push({ text: "no keys", kind: "" });
    else if (u.keys > 0) out.push({ text: `${u.keys} key${u.keys === 1 ? "" : "s"}`, kind: "ok" });
    if (u.keys_error) out.push({ text: "keys unreadable", kind: "warn" });
    if (!u.can_login) out.push({ text: "cannot log in", kind: "warn" });
    if (!u.home_exists) out.push({ text: "no home folder", kind: "warn" });
  }
  return out;
}

const date = (ts) => fmtDateTime(ts);

/** [[label, text]] for the details dialog. */
export function accountDetails(u, sessions = [], now = Date.now() / 1000) {
  const sudo = !u.sudo ? "no" : `${u.sudo === "full" ? "yes, all commands" : "limited commands"}${u.nopasswd ? ", without a password" : ""}`
    + (u.sudo_via && u.sudo_via.length ? ` (${u.sudo_via.join(", ")})` : "");
  const login = u.last_login ? `${date(u.last_login.time)} (${fmtAgo(u.last_login.time, now)}) from ${u.last_login.from || "this machine"}${u.last_login.tty ? ` on ${u.last_login.tty}` : ""}` : "never";
  const keys = u.keys === null || u.keys === undefined ? (u.keys_error ? `cannot be checked: ${u.keys_error}` : "not checked for this kind of account") : String(u.keys);
  const home = u.home_exists
    ? `${u.home} (owner ${u.home_owner}, mode ${u.home_mode}${u.home_owner_ok === false ? ", NOT owned by this user" : ""})`
    : `${u.home} (does not exist)`;
  const password = u.password === "unknown" ? PASSWORD.unknown : PASSWORD[u.password] || u.password;
  const changed = u.must_change ? "must be changed at the next login" : u.password_changed ? date(u.password_changed) : "unknown";
  const open = sessions.filter((s) => s.user === u.name)
    .map((s) => `#${s.id} from ${s.from || "?"} via ${s.service || "?"}, ${s.since ? fmtDuration(now - s.since) : "?"}`);
  return [
    ["Account", `${u.name} (user ${u.uid}, group ${u.primary_group})`],
    ["Full name", u.comment || ""],
    ["Type", TYPES[u.type] || u.type],
    ["Can log in", u.can_login ? "yes" : `no: ${u.blockers.length ? u.blockers.join("; ") : "system account"}`],
    ["Sudo", sudo],
    ["Password", password],
    ["Password changed", u.password === "unknown" ? "" : changed],
    ["Password expires", u.password_expires ? `${date(u.password_expires)}${u.password_expired ? " (already)" : ""}` : "never"],
    ["Account expires", u.expires ? `${date(u.expires)}${u.expired ? " (already)" : ""}` : "never"],
    ["Last login", login],
    ["SSH keys", keys],
    ["Groups", (u.groups || []).join(", ")],
    ["Home folder", home],
    ["Shell", u.shell],
    ["Running processes", String(u.processes ?? 0)],
    ["Signed in now", open.length ? open.join("\n") : "no"],
  ];
}

/** How a logind session looks in the table: "sshd · pts/1" or "console · tty1". */
export function sessionVia(s) {
  const how = s.service === "sshd" ? "SSH" : s.service === "login" ? "console" : s.service || "?";
  return s.tty ? `${how} · ${s.tty}` : how;
}
