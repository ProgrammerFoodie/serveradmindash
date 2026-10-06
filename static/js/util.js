// Small DOM and formatting helpers. Text always goes in as text nodes, never as HTML: much of what
// this dashboard shows (process names, log lines, user agents, usernames tried against SSH) is
// controlled by strangers.

// For these, the mere presence of the attribute means "on", so only the truthiness of the value may decide.
// (An empty string is falsy in JavaScript but would still switch the attribute on.)
const FLAGS = new Set(["disabled", "hidden", "checked", "autofocus", "selected", "readonly", "required", "open"]);

export function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (FLAGS.has(key)) { if (value) node.setAttribute(key, ""); continue; }
    if (value == null || value === false || key === "style") continue; // inline styles are blocked by the CSP anyway
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  append(node, children);
  return node;
}

export function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) { node.replaceChildren(); return node; }

/** replaceChildren that skips null/false (the native one would print the word "null"). */
export function fill(node, ...children) {
  node.replaceChildren(...children.flat(Infinity).filter((c) => c != null && c !== false));
  return node;
}

// ---- number and time formatting ---------------------------------------------------------------

const DASH = "–";
export const isNum = (v) => typeof v === "number" && Number.isFinite(v);

export function fmtNum(v, digits = 0) {
  return isNum(v) ? v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits }) : DASH;
}

export function fmtPct(v, digits = 1) { return isNum(v) ? `${v.toFixed(digits)}%` : DASH; }

export function fmtBytes(v, digits) {
  if (!isNum(v)) return DASH;
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let n = Math.abs(v), i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  const d = digits ?? (i === 0 || n >= 100 ? 0 : n >= 10 ? 1 : 2);
  return `${v < 0 ? "-" : ""}${n.toFixed(d)} ${units[i]}`;
}

export function fmtRate(v) { return isNum(v) ? `${fmtBytes(v)}/s` : DASH; }

export function fmtDuration(seconds) {
  if (!isNum(seconds)) return DASH;
  let s = Math.max(0, Math.round(seconds));
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

export function fmtAgo(ts, now = Date.now() / 1000) {
  if (!isNum(ts)) return DASH;
  const d = now - ts;
  if (d < 0) return fmtUntil(ts, now);
  if (d < 5) return "just now";
  if (d < 90) return `${Math.round(d)}s ago`;
  if (d < 5400) return `${Math.round(d / 60)} min ago`;
  if (d < 129600) return `${Math.round(d / 3600)} h ago`;
  return `${Math.round(d / 86400)} d ago`;
}

export function fmtUntil(ts, now = Date.now() / 1000) {
  if (!isNum(ts)) return DASH;
  const d = ts - now;
  if (d <= 0) return "now";
  if (d < 90) return `in ${Math.round(d)}s`;
  if (d < 5400) return `in ${Math.round(d / 60)} min`;
  if (d < 129600) return `in ${Math.round(d / 3600)} h`;
  return `in ${Math.round(d / 86400)} d`;
}

export function fmtTime(ts) {
  return isNum(ts) ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : DASH;
}

export function fmtDateTime(ts) {
  return isNum(ts)
    ? new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
    : DASH;
}

// ---- thresholds -------------------------------------------------------------------------------

/** "ok" | "warn" | "crit" for a value against {warn, crit}; lowerIsWorse flips the comparison. */
export function level(rule, value, lowerIsWorse = false) {
  if (!rule || !isNum(value)) return "ok";
  if (lowerIsWorse) return value <= rule.crit ? "crit" : value <= rule.warn ? "warn" : "ok";
  return value >= rule.crit ? "crit" : value >= rule.warn ? "warn" : "ok";
}

// ---- small components -------------------------------------------------------------------------

export function badge(text, lvl = "") { return el("span", { class: `badge ${lvl}` }, text); }

export function bar(pct, lvl = "ok", thin = false) {
  const fill = el("i");
  fill.style.width = `${Math.max(0, Math.min(100, isNum(pct) ? pct : 0))}%`; // CSSOM is allowed by the CSP, style="" is not
  return el("div", { class: `bar ${lvl} ${thin ? "thin" : ""}`, role: "img", "aria-label": `${fmtPct(pct, 0)}` }, fill);
}

export function card(title, ...children) {
  return el("section", { class: "card" }, title == null ? null : el("h3", null, title), children);
}

export function kv(pairs) {
  const dl = el("dl", { class: "kv" });
  for (const [k, v] of pairs) if (v !== undefined) dl.append(el("dt", null, k), el("dd", null, v));
  return dl;
}

/** Replace a card's contents with a message when its collector failed or has not reported yet. */
export function unavailable(section, why) {
  return el("p", { class: why ? "err" : "empty" }, why ? `Unavailable: ${why}` : "Waiting for data…");
}

/**
 * A table whose rows are replaced on every update, with click-to-sort headers.
 * columns: [{key, label, num, cls, render(row) -> Node|string, value(row) -> sortable, sortable}]
 */
export function dataTable({ columns, sortKey, sortDir = -1, empty = "Nothing to show", rowClass, onRowClick }) {
  const state = { key: sortKey, dir: sortDir, rows: [] };
  const head = el("tr");
  const body = el("tbody");
  const table = el("table", null, el("thead", null, head), body);
  const wrap = el("div", { class: "table-wrap" }, table);

  const valueOf = (col, row) => (col.value ? col.value(row) : row[col.key]);

  function paintHead() {
    head.replaceChildren(...columns.map((col) => {
      const sortable = col.sortable !== false;
      const th = el("th", { class: `${col.num ? "num" : ""} ${sortable ? "sortable" : ""}`, scope: "col",
                            "aria-sort": state.key === col.key ? (state.dir > 0 ? "ascending" : "descending") : null,
                            tabindex: sortable ? 0 : null }, col.label);
      if (sortable) {
        const toggle = () => {
          state.dir = state.key === col.key ? -state.dir : (col.num ? -1 : 1);
          state.key = col.key;
          paintHead(); paintBody();
        };
        th.addEventListener("click", toggle);
        th.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
      }
      return th;
    }));
  }

  function paintBody() {
    const col = columns.find((c) => c.key === state.key);
    let rows = state.rows;
    if (col) {
      rows = [...rows].sort((a, b) => {
        const x = valueOf(col, a), y = valueOf(col, b);
        if (x == null && y == null) return 0;
        if (x == null) return 1;                    // empty values always last
        if (y == null) return -1;
        return (typeof x === "string" ? x.localeCompare(y) : x - y) * state.dir;
      });
    }
    if (!rows.length) {
      body.replaceChildren(el("tr", null, el("td", { colspan: columns.length, class: "empty" }, empty)));
      return;
    }
    body.replaceChildren(...rows.map((row) => {
      const tr = el("tr", { class: `${rowClass ? rowClass(row) : ""} ${onRowClick ? "clickable" : ""}` },
        columns.map((c) => {
          const content = c.render ? c.render(row) : row[c.key];
          return el("td", { class: `${c.num ? "num" : ""} ${c.cls || ""}` }, content == null || content === "" ? DASH : content);
        }));
      if (onRowClick) tr.addEventListener("click", () => onRowClick(row));
      return tr;
    }));
  }

  paintHead();
  paintBody();
  return { el: wrap, setRows(rows) { state.rows = rows || []; paintBody(); } };
}

/** Settings that should survive a reload but must never break the page if storage is unavailable. */
export const store = {
  get(key, fallback) { try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode or blocked */ } },
};

/** A card whose body is replaced on every update. */
export function panel(title, extra) {
  const body = el("div");
  const root = card(title, body);
  if (extra) root.querySelector("h3").append(el("span", { class: "grow" }), extra);
  return { el: root, set: (...nodes) => body.replaceChildren(...nodes.flat(Infinity).filter((n) => n != null && n !== false)) };
}

/** Fill a panel from a live section: waiting, collector error, or rendered content. */
export function paint(p, section, render) {
  if (!section) return p.set(unavailable());
  if (section.data && section.data.error) return p.set(unavailable(null, section.data.error));
  try { p.set(render(section.data)); } catch (e) { console.error(e); p.set(unavailable(null, "unexpected data")); }
}
