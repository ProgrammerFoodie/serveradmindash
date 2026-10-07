// The confirm dialog, plain and "type the word": which button has the focus, when the confirm button is enabled,
// and what Enter does. Runs against the fake DOM, so no browser is needed.

import { installFakeDom, FakeNode } from "./fakedom.mjs";

let failed = 0;
const check = (ok, label) => { if (!ok) { console.log(`FAIL ${label}`); failed++; } };

installFakeDom();
const { askConfirm, typedMatches } = await import("../../static/js/confirm.js");

function makeDialog() {
  const dialog = new FakeNode("dialog");
  dialog.showModal = () => { dialog.open = true; };
  return dialog;
}
/** What the browser does when a button inside <form method="dialog"> is pressed. */
const press = (dialog, button) => { dialog.returnValue = button.attrs.value ?? ""; dialog.fire("close"); };
const buttons = (dialog) => dialog.find((n) => n.tag === "button");
const byLabel = (dialog, label) => buttons(dialog).find((b) => b.textContent === label);
const type = (input, text) => { input.value = text; input.fire("input"); };

// ---- plain confirmation
{
  const dialog = makeDialog();
  const answer = askConfirm(dialog, { title: "Restart nginx?", lines: ["a line"], warning: ["careful"], confirmLabel: "Restart", danger: true });
  check(dialog.open === true, "the dialog opens");
  check(dialog.find((n) => n.tag === "input").length === 0, "plain confirmation has no text field");
  check("autofocus" in byLabel(dialog, "Cancel").attrs && !("autofocus" in byLabel(dialog, "Restart").attrs), "Cancel has the focus, not the confirm button");
  check(byLabel(dialog, "Restart").hasClass("danger") && !byLabel(dialog, "Restart").disabled, "a danger button is enabled and styled");
  check(dialog.textContent.includes("Restart nginx?") && dialog.textContent.includes("careful"), "title and warning are shown");
  press(dialog, byLabel(dialog, "Restart"));
  check(await answer === true, "pressing confirm resolves true");
}
{
  const dialog = makeDialog();
  const answer = askConfirm(dialog, { title: "x" });
  press(dialog, byLabel(dialog, "Cancel"));
  check(await answer === false, "pressing Cancel resolves false");
}
{
  const dialog = makeDialog();
  const answer = askConfirm(dialog, { title: "x" });
  dialog.returnValue = "";
  dialog.fire("close");                                   // Escape closes the dialog without a button
  check(await answer === false, "Escape resolves false");
}

// ---- typed confirmation
{
  const dialog = makeDialog();
  const answer = askConfirm(dialog, { title: "Reboot?", typed: "myhost", confirmLabel: "Reboot", danger: true });
  const input = dialog.find((n) => n.tag === "input")[0], ok = byLabel(dialog, "Reboot");
  check(ok.disabled, "the confirm button starts disabled");
  check("autofocus" in input.attrs && !("autofocus" in byLabel(dialog, "Cancel").attrs), "the text field has the focus instead of Cancel");
  check(dialog.textContent.includes("Type myhost to confirm"), "it says what to type");
  type(input, "myhos"); check(ok.disabled, "a partial word does not enable it");
  type(input, "MYHOST"); check(ok.disabled, "the wrong case does not enable it");
  type(input, "myhost2"); check(ok.disabled, "a longer word does not enable it");
  type(input, "myhost"); check(!ok.disabled, "the exact word enables it");
  type(input, "  myhost \n"); check(!ok.disabled, "spaces around a pasted word are ignored");
  type(input, "myhos"); check(ok.disabled, "deleting a letter disables it again");

  let clicked = 0;
  ok.click = () => { clicked++; press(dialog, ok); };
  const enterWrong = input.fire("keydown", { key: "Enter" });
  check(enterWrong.defaultPrevented && clicked === 0, "Enter with the wrong word confirms nothing and does not fall through to Cancel");
  type(input, "myhost");
  input.fire("keydown", { key: "Enter" });
  check(clicked === 1 && await answer === true, "Enter with the right word confirms");
}
{
  const dialog = makeDialog();
  const answer = askConfirm(dialog, { title: "x", typed: "word" });
  type(dialog.find((n) => n.tag === "input")[0], "word");
  press(dialog, byLabel(dialog, "Cancel"));
  check(await answer === false, "Cancel still cancels after typing the word");
}
{
  // a second dialog in the same element starts clean: no leftover word, no stale answer
  const dialog = makeDialog();
  const first = askConfirm(dialog, { title: "one", typed: "word" });
  type(dialog.find((n) => n.tag === "input")[0], "word");
  press(dialog, byLabel(dialog, "Cancel"));
  await first;
  const second = askConfirm(dialog, { title: "two", typed: "word" });
  check(byLabel(dialog, "Confirm").disabled && dialog.find((n) => n.tag === "input")[0].value === "", "a new dialog starts with nothing typed and confirm disabled");
  press(dialog, byLabel(dialog, "Cancel"));
  check(await second === false, "and resolves on its own");
}

// ---- the word check itself
for (const [value, word, expected] of [["a", "a", true], [" a ", "a", true], ["A", "a", false], ["", "", false], ["x", "", false], [null, "a", false], [undefined, "a", false], [5, "a", false], ["a", null, false]]) {
  check(typedMatches(value, word) === expected, `typedMatches(${JSON.stringify(value)}, ${JSON.stringify(word)})`);
}

if (failed) { console.log(`${failed} check(s) failed`); process.exit(1); }
console.log("confirm smoke ok");
