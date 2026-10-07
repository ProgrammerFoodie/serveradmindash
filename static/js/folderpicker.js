// A folder browser, so a home folder can be chosen instead of typed from memory. It asks the server for one folder at a time
// (GET /api/folders): places to start from (with free space), a clickable path, the sub-folders, and for each one that cannot be
// used the reason. The result is "the folder you are in" or "a new folder inside it" (the name box, prefilled).
// The server decides what is allowed; this only shows it, and the action that follows checks everything again.

import { append, clear, el, fmtBytes } from "./util.js";

/** [{label, path}] for "/mnt/Extra20" -> "/", "mnt", "Extra20", each with the path it leads to. */
export function crumbs(path) {
  const out = [{ label: "/", path: "/" }];
  let at = "";
  for (const part of String(path || "").split("/").filter(Boolean)) {
    at += `/${part}`;
    out.push({ label: part, path: at });
  }
  return out;
}

/** The folder that would be chosen: the current one, or a (new) folder inside it. */
export const previewPath = (dir, name) => (name ? (dir === "/" ? `/${name}` : `${dir}/${name}`) : dir);

/** The same rule the server applies to a folder name, so Choose can stay disabled for an obviously bad one. */
export function nameProblem(name) {
  if (!name) return null;                                          // empty means "this folder itself"
  if (name.length > 64) return "A folder name is at most 64 characters.";
  if (name === "." || name === ".." || /[/\u0000-\u001f\u007f]/.test(name) || name !== name.trim()) return "A folder name cannot contain / or control characters, or be . or ..";
  return null;
}

const url = (path, hidden, forUser, name) => `/api/folders?path=${encodeURIComponent(path || "")}&hidden=${hidden ? 1 : 0}`
  + (forUser ? `&for=${encodeURIComponent(forUser)}` : "") + (name !== undefined ? `&name=${encodeURIComponent(name)}` : "");

/**
 * Show the picker in `dialog` and resolve with the chosen path, or null if cancelled.
 * options: { start: folder to open first (falls back to the first place), name: prefilled folder name, forUser: account the folder is for }
 */
export function pickFolder(dialog, api, { start = "", name = "", forUser = "" } = {}) {
  return new Promise((resolve) => {
    let data = null, hidden = false, nameValue = name, chosen = null, busy = false;
    const body = el("div", { class: "picker-body" });
    const error = el("p", { class: "form-error", role: "alert", hidden: true });
    const nameInput = el("input", { type: "text", name: "folder-name", value: name, autocomplete: "off", autocapitalize: "none", spellcheck: "false", "aria-label": "Folder name" });
    const choose = el("button", { class: "btn primary", type: "button", disabled: true, onclick: () => accept() }, "Choose this folder");
    const preview = el("p", { class: "folder-preview" });
    const note = el("small", { class: "form-help" });

    function say(text) { error.textContent = text; error.hidden = !text; }

    function paintChoice() {
      const bad = nameProblem(nameValue);
      const target = data ? previewPath(data.path, nameValue) : "";
      const exists = !!nameValue && !!data && data.folders.some((f) => f.name === nameValue);
      const blocked = bad || (!nameValue && data && !data.selectable ? data.reason || "This folder cannot be used." : null);
      append(clear(preview), ["The home folder will be: ", el("b", { class: "mono" }, target || "…")]);
      note.textContent = bad || (blocked && !nameValue ? blocked : exists ? "A folder with this name is already here." : nameValue ? "It is created for the user." : "");
      choose.disabled = busy || !data || !!blocked;
    }

    function row(f) {
      const label = [el("span", { class: "folder-icon", "aria-hidden": "true" }, "▸"), el("span", { class: "folder-name" }, f.name)];
      if (f.enterable) return el("button", { class: "folder-row", type: "button", onclick: () => load(f.path) }, label, f.selectable ? null : el("small", { class: "muted" }, f.reason));
      return el("div", { class: "folder-row disabled", "aria-disabled": "true" }, label, el("small", { class: "muted" }, f.reason));
    }

    function paint() {
      append(clear(body), [
        el("div", { class: "places" }, data.places.map((p) => el("button", { class: `place ${p.path === data.path ? "current" : ""}`, type: "button", onclick: () => load(p.path),
          title: p.device ? `${p.device}${p.writable ? "" : " (read-only for the dashboard)"}` : "" },
        el("b", { class: "mono" }, p.path), el("small", { class: "muted" }, p.free == null ? "" : `${fmtBytes(p.free)} free`), p.writable ? null : el("small", { class: "err" }, "read-only")))),
        el("div", { class: "crumbs", role: "navigation", "aria-label": "Folder path" }, crumbs(data.path).map((c, i, all) =>
          el("button", { class: "crumb", type: "button", disabled: i === all.length - 1, onclick: () => load(c.path) }, c.label))),
        data.writable ? null : el("p", { class: "err" }, "The dashboard cannot write in this folder (it is read-only to its service), so a home cannot be created or moved here."),
        el("div", { class: "folder-list", role: "list" }, data.folders.length ? data.folders.map(row) : el("p", { class: "empty" }, hidden ? "No folders here." : "No folders here (hidden ones are not shown).")),
        data.truncated ? el("p", { class: "sub" }, "Only the first folders are shown; type a name to pick one that is not listed.") : null]);
      paintChoice();
    }

    async function load(path) {
      busy = true;
      paintChoice();
      try {
        data = await api.get(url(path, hidden, forUser));
        say("");
        paint();
      } catch (e) {
        say(e.message);
        if (!data && path) await load("");                       // the starting folder was no good: begin at the first place instead
      } finally {
        busy = false;
        paintChoice();
      }
    }

    async function accept() {
      if (!data) return;
      if (!nameValue) { chosen = data.path; dialog.close("ok"); return; }
      busy = true;
      paintChoice();
      try {
        const answer = await api.get(url(data.path, hidden, forUser, nameValue));
        if (!answer.choice.ok) { say(answer.choice.reason || "That folder cannot be used."); return; }
        chosen = answer.choice.path;
        dialog.close("ok");
      } catch (e) {
        say(e.message);
      } finally {
        busy = false;
        paintChoice();
      }
    }

    nameInput.addEventListener("input", () => { nameValue = nameInput.value; say(""); paintChoice(); });
    const showHidden = el("input", { type: "checkbox", onchange: (e) => { hidden = !!e.target.checked; load(data ? data.path : ""); } });
    dialog.replaceChildren(el("div", { class: "picker" },
      el("h2", { id: "picker-title" }, "Choose a folder"),
      body,
      el("label", { class: "form-check" }, showHidden, el("span", null, "Show hidden folders")),
      el("label", { class: "form-row" }, el("span", null, "Folder name"), nameInput, note),
      preview, error,
      el("div", { class: "confirm-actions" }, el("button", { class: "btn", type: "button", onclick: () => dialog.close("cancel") }, "Cancel"), choose)));
    dialog.addEventListener("close", () => {
      dialog.replaceChildren();
      resolve(dialog.returnValue === "ok" ? chosen : null);
    }, { once: true });
    dialog.returnValue = "";
    dialog.showModal();
    load(start);
  });
}
