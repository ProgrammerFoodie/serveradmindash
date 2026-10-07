// The "are you sure?" dialog. Plain confirmation, or "type this word to confirm" for the dangerous actions
// (reboot, removing a user ...). The server checks a typed word again, so this is only for the person's benefit.

import { el } from "./util.js";

/** True when what was typed is exactly the word (surrounding spaces from a copy-paste are ignored). */
export const typedMatches = (value, word) => typeof value === "string" && typeof word === "string" && word !== "" && value.trim() === word;

/**
 * Show `dialog` (a <dialog> element) and resolve true only if the person pressed the confirm button.
 * Cancel is the default button, so Enter or a stray click never confirms; with `typed`, the confirm button
 * stays disabled until the word has been typed, and the text field is focused instead.
 */
export function askConfirm(dialog, { title, lines = [], warning = [], confirmLabel = "Confirm", danger = false, typed = null }) {
  return new Promise((resolve) => {
    dialog.returnValue = "";
    const ok = el("button", { class: `btn ${danger ? "danger" : "primary"}`, value: "ok", disabled: !!typed }, confirmLabel);
    const input = typed ? el("input", { type: "text", class: "confirm-typed", autocomplete: "off", autocapitalize: "none", spellcheck: "false",
      "aria-label": `Type ${typed} to confirm`, autofocus: true,
      oninput: () => { ok.disabled = !typedMatches(input.value, typed); },
      onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); if (!ok.disabled) ok.click(); } } }) : null;
    dialog.replaceChildren(el("form", { method: "dialog" },
      el("h2", { id: "confirm-title" }, title),
      lines.map((l) => el("p", null, l)), warning.map((l) => el("p", { class: "warn" }, l)),
      typed ? el("label", { class: "confirm-label" }, el("span", null, "Type ", el("b", null, typed), " to confirm"), input) : null,
      el("div", { class: "confirm-actions" },
        el("button", { class: "btn", value: "cancel", autofocus: !typed }, "Cancel"), ok)));
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
    dialog.showModal();
  });
}
