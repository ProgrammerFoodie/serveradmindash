// A small form in the confirm dialog: text, password, checkbox and select fields, checked before it can be sent, and
// optionally "type this word to confirm". The page checks only for the person's benefit; the server checks everything again.
// Passwords are taken out of the page as soon as the dialog closes.

import { typedMatches } from "./confirm.js";
import { el } from "./util.js";

/**
 * fields: [{ name, label, type: "text"|"password"|"checkbox"|"select", value, placeholder, options: [{value, label}],
 *            required, minLength, maxLength, pattern, patternMessage, matches, help, autocomplete }]
 * `matches` names another field that must hold the same text (the repeat-the-password field).
 * Returns the first problem as a sentence, or null.
 */
export function formProblem(fields, values) {
  for (const f of fields) {
    const v = values[f.name];
    if (f.type === "checkbox") continue;
    const text = typeof v === "string" ? v : "";
    if (f.required && !text.trim()) return `${f.label} is required.`;
    if (f.matches && text !== (typeof values[f.matches] === "string" ? values[f.matches] : "")) return "The two passwords do not match.";   // also when this one is empty
    if (!text) continue;
    if (f.minLength && text.length < f.minLength) return `${f.label} must be at least ${f.minLength} characters.`;
    if (f.maxLength && text.length > f.maxLength) return `${f.label} can be at most ${f.maxLength} characters.`;
    if (f.pattern && !new RegExp(`^(?:${f.pattern})$`).test(text)) return f.patternMessage || `${f.label} is not in the right form.`;
  }
  return null;
}

/** Show the form in `dialog`. Resolves with {name: value} when sent, or null when cancelled. */
export function askForm(dialog, { title, lines = [], warning = [], fields, submitLabel = "Save", danger = false, typed = null }) {
  return new Promise((resolve) => {
    dialog.returnValue = "";
    const inputs = new Map();
    const rows = fields.map((f) => {
      let input;
      if (f.type === "select") {
        input = el("select", { name: f.name, "aria-label": f.label }, (f.options || []).map((o) => el("option", { value: o.value }, o.label)));
        input.value = f.value ?? (f.options && f.options[0] ? f.options[0].value : "");
      } else if (f.type === "checkbox") {
        input = el("input", { type: "checkbox", name: f.name });
        input.checked = !!f.value;
      } else {
        input = el("input", { type: f.type === "password" ? "password" : "text", name: f.name, value: f.value ?? "", placeholder: f.placeholder || "",
          autocomplete: f.autocomplete || (f.type === "password" ? "new-password" : "off"), autocapitalize: "none", spellcheck: "false", "aria-label": f.label });
      }
      inputs.set(f.name, input);
      const help = f.help ? el("small", { class: "form-help" }, f.help) : null;
      return f.type === "checkbox"
        ? el("label", { class: "form-check" }, input, el("span", null, f.label), help)
        : el("label", { class: "form-row" }, el("span", null, f.label), input, help);
    });

    const typedInput = typed ? el("input", { type: "text", class: "confirm-typed", autocomplete: "off", autocapitalize: "none", spellcheck: "false", "aria-label": `Type ${typed} to confirm` }) : null;
    const send = el("button", { class: `btn ${danger ? "danger" : "primary"}`, type: "submit", disabled: !!typed }, submitLabel);
    const error = el("p", { class: "form-error", role: "alert", hidden: true });
    if (typedInput) typedInput.addEventListener("input", () => { send.disabled = !typedMatches(typedInput.value, typed); });

    const kind = Object.fromEntries(fields.map((f) => [f.name, f.type]));
    const read = () => Object.fromEntries([...inputs].map(([name, input]) => [name, kind[name] === "checkbox" ? !!input.checked : input.value]));
    let result = null;
    const form = el("form", { class: "form-dialog", method: "dialog", novalidate: true,
      onsubmit: (e) => {
        e.preventDefault();
        if (typed && !typedMatches(typedInput.value, typed)) return;
        const values = read();
        const problem = formProblem(fields, values);
        if (problem) { error.textContent = problem; error.hidden = false; return; }
        result = values;
        dialog.returnValue = "ok";
        dialog.close("ok");
      } },
      el("h2", { id: "confirm-title" }, title),
      lines.map((l) => el("p", null, l)), warning.map((l) => el("p", { class: "warn" }, l)),
      rows,
      typed ? el("label", { class: "confirm-label" }, el("span", null, "Type ", el("b", null, typed), " to confirm"), typedInput) : null,
      error,
      el("div", { class: "confirm-actions" }, el("button", { class: "btn", type: "button", onclick: () => dialog.close("cancel") }, "Cancel"), send));

    dialog.replaceChildren(form);
    dialog.addEventListener("close", () => {
      dialog.replaceChildren();                                  // the typed passwords leave the page with the dialog
      for (const [name, input] of inputs) if (kind[name] === "password") input.value = "";
      resolve(dialog.returnValue === "ok" ? result : null);
    }, { once: true });
    dialog.showModal();
  });
}
