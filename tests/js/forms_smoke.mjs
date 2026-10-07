// The form dialog and the account action flows, against the fake DOM and a fake page context.

import { FakeNode, installFakeDom } from "./fakedom.mjs";

let failed = 0;
const check = (ok, label) => { if (!ok) { console.log(`FAIL ${label}`); failed++; } };
installFakeDom();

const { askForm, formProblem } = await import("../../static/js/formdialog.js");
const actions = await import("../../static/js/useractions.js");

// ---- formProblem: the rules, one at a time
const F = [
  { name: "name", label: "User name", type: "text", required: true, pattern: "[a-z_][a-z0-9_-]{0,31}", patternMessage: "bad name" },
  { name: "password", label: "Password", type: "password", minLength: 10, maxLength: 20 },
  { name: "password2", label: "Repeat", type: "password", matches: "password" },
  { name: "sudo", label: "Sudo", type: "checkbox" },
];
const values = (over) => ({ name: "dave", password: "", password2: "", sudo: false, ...over });
check(formProblem(F, values()) === null, "a valid form has no problem");
check(formProblem(F, values({ name: "" })) === "User name is required.", "required");
check(formProblem(F, values({ name: "   " })) === "User name is required.", "blank is empty");
check(formProblem(F, values({ name: "Dave" })) === "bad name", "pattern with its message");
check(formProblem(F, values({ name: "dave x" })) === "bad name", "the whole value must match, not a part of it");
check(formProblem(F, values({ password: "short", password2: "short" })).includes("at least 10"), "minimum length");
check(formProblem(F, values({ password: "x".repeat(21), password2: "x".repeat(21) })).includes("at most 20"), "maximum length");
check(formProblem(F, values({ password: "0123456789", password2: "0123456780" })) === "The two passwords do not match.", "the repeat must match");
check(formProblem(F, values({ password: "0123456789", password2: "" })) === "The two passwords do not match.", "an empty repeat does not match a password");
check(formProblem(F, values({ password: "", password2: "" })) === null, "an optional empty password is fine");
check(formProblem(F, values({ password: "0123456789", password2: "0123456789" })) === null, "matching passwords are fine");
check(formProblem(F, { name: "dave" }) === null, "missing optional values are fine");

// ---- askForm
function makeDialog() {
  const dialog = new FakeNode("dialog");
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = (value) => { if (value !== undefined) dialog.returnValue = value; dialog.open = false; dialog.fire("close"); };
  return dialog;
}
const field = (dialog, name) => dialog.find((n) => n.attrs && n.attrs.name === name)[0];
const button = (dialog, label) => dialog.find((n) => n.tag === "button" && n.textContent === label)[0];
const submit = (dialog) => dialog.find((n) => n.tag === "form")[0].fire("submit");
const errorText = (dialog) => { const e = dialog.find((n) => n.hasClass("form-error"))[0]; return e && !e.hidden ? e.textContent : ""; };

const SPEC = { title: "Add a user", submitLabel: "Add", fields: [
  { name: "name", label: "User name", type: "text", required: true, pattern: "[a-z]+", patternMessage: "letters only" },
  { name: "shell", label: "Shell", type: "select", options: [{ value: "/bin/sh", label: "sh" }, { value: "/bin/bash", label: "bash" }], value: "/bin/bash" },
  { name: "sudo", label: "Sudo", type: "checkbox" },
  { name: "password", label: "Password", type: "password", minLength: 10, autocomplete: "new-password" },
  { name: "password2", label: "Repeat", type: "password", matches: "password" },
] };
{
  const dialog = makeDialog();
  const answer = askForm(dialog, SPEC);
  check(dialog.open && dialog.textContent.includes("Add a user"), "the dialog opens with its title");
  check(field(dialog, "password").attrs.type === "password" && field(dialog, "password").attrs.autocomplete === "new-password", "password fields are real password fields that no manager fills in");
  check(field(dialog, "shell").value === "/bin/bash", "the select starts on its default");
  submit(dialog);
  check(errorText(dialog) === "User name is required." && dialog.open, "an invalid form stays open and says why");
  field(dialog, "name").value = "dave"; field(dialog, "password").value = "0123456789"; field(dialog, "password2").value = "nope";
  submit(dialog);
  check(errorText(dialog) === "The two passwords do not match." && dialog.open, "mismatched passwords are caught");
  field(dialog, "password2").value = "0123456789"; field(dialog, "sudo").checked = true; field(dialog, "shell").value = "/bin/sh";
  const pw = field(dialog, "password");
  submit(dialog);
  const result = await answer;
  check(result && result.name === "dave" && result.password === "0123456789" && result.sudo === true && result.shell === "/bin/sh", `the values come back: ${JSON.stringify(result)}`);
  check(!dialog.open && dialog.children.length === 0, "the dialog is emptied when it closes");
  check(pw.value === "", "and the typed password is wiped from its field");
}
{
  const dialog = makeDialog();
  const answer = askForm(dialog, SPEC);
  field(dialog, "name").value = "dave"; field(dialog, "password").value = "0123456789";
  const pw = field(dialog, "password");
  button(dialog, "Cancel").click(); dialog.close("cancel");
  check(await answer === null && pw.value === "", "cancel gives null and still wipes the password");
}
{
  const dialog = makeDialog();
  const answer = askForm(dialog, SPEC);
  dialog.returnValue = ""; dialog.fire("close");                                       // Escape
  check(await answer === null, "Escape gives null");
}
{
  const dialog = makeDialog();
  const answer = askForm(dialog, { title: "Remove", submitLabel: "Remove it", danger: true, typed: "dave", fields: [{ name: "delete_home", label: "Delete home", type: "checkbox" }] });
  const send = button(dialog, "Remove it"), typedField = dialog.find((n) => n.hasClass("confirm-typed"))[0];
  check(send.disabled && send.hasClass("danger"), "the typed variant starts disabled");
  submit(dialog);
  check(dialog.open, "submitting with the word missing does nothing, even by Enter");
  typedField.value = "dav"; typedField.fire("input");
  check(send.disabled, "a partial word keeps it disabled");
  typedField.value = "dave"; typedField.fire("input");
  check(!send.disabled, "the right word enables it");
  field(dialog, "delete_home").checked = true;
  submit(dialog);
  const result = await answer;
  check(result && result.delete_home === true, "and the values come back");
}
{
  const dialog = makeDialog();
  askForm(dialog, { title: "x", fields: [{ name: "a", label: "A", type: "text", value: "initial" }] });
  check(field(dialog, "a").value === "initial", "text fields start with their value");
}

// ---- the folder field with Browse…
{
  const dialog = makeDialog();
  const calls = [];
  const answer = askForm(dialog, { title: "Home", submitLabel: "Go", browse: async (name, values) => { calls.push([name, values]); return calls.length === 1 ? "/mnt/Extra20/bob" : null; },
    fields: [{ name: "name", label: "Name", type: "text", value: "bob" }, { name: "path", label: "Folder", type: "folder", value: "/home/x", required: true }] });
  const pathInput = field(dialog, "path"), browse = button(dialog, "Browse…");
  check(!!pathInput && !!browse && !browse.disabled, "a folder field is a text box with a Browse… button");
  browse.click(); await new Promise((r) => setTimeout(r, 10));
  check(JSON.stringify(calls[0]) === JSON.stringify(["path", { name: "bob", path: "/home/x" }]), `Browse… is given the field and the current values: ${JSON.stringify(calls[0])}`);
  check(pathInput.value === "/mnt/Extra20/bob", "what the picker returns goes into the box");
  browse.click(); await new Promise((r) => setTimeout(r, 10));
  check(pathInput.value === "/mnt/Extra20/bob", "cancelling the picker leaves the box alone");
  pathInput.value = "/typed/by/hand";
  submit(dialog);
  const result = await answer;
  check(result && result.path === "/typed/by/hand", "a path typed by hand still works");
}
{
  const dialog = makeDialog();
  askForm(dialog, { title: "Home", fields: [{ name: "path", label: "Folder", type: "folder" }] });
  check(button(dialog, "Browse…").disabled, "without a picker the button is off, the box still works");
}

// ---- which buttons an account gets
const user = (over) => ({ name: "bob", type: "login", password: "set", expired: false, home: "/home/bob", ...over });
const ids = (u) => actions.accountActions(u).map((a) => a.id);
check(ids(user()).join() === "password,lock,ban,rename,home,remove", `a normal login user: ${ids(user())}`);
check(ids(user({ expired: true })).join() === "password,unlock,rename,home,remove", "an expired account offers Unlock instead of Lock and Ban");
check(ids(user({ password: "locked" })).join() === "password,unlock,rename,home,remove", "so does a locked password");
check(ids({ type: "root", name: "root" }).join() === "password", "root only gets a new password");
check(ids({ type: "system", name: "daemon" }).length === 0, "system accounts get nothing");
check(actions.accountActions(user()).filter((a) => a.danger).map((a) => a.id).join() === "ban,remove", "only ban and remove look dangerous");

// ---- the flows
function makeCtx({ confirm = true, form = {} } = {}) {
  const log = { confirms: [], typed: [], forms: [], sent: [], picks: [] };
  return { log, ctx: {
    confirm: async (o) => { log.confirms.push(o); return confirm; },
    confirmTyped: async (o) => { log.typed.push(o); return confirm; },
    askForm: async (o) => { log.forms.push(o); return form === null ? null : (typeof form === "function" ? form(o) : form); },
    pickFolder: async (o) => { log.picks.push(o); return "/mnt/Extra20/chosen"; },
    act: async (path, body) => { log.sent.push([path, body]); return { ok: true }; },
  } };
}
const sent = (log) => JSON.stringify(log.sent);

{
  const { ctx, log } = makeCtx();
  check(await actions.runAction(ctx, "lock", user()) === true && sent(log) === JSON.stringify([["/api/users/lock", { name: "bob" }]]), "lock asks once, then sends the name");
  check(log.confirms[0].title === "Lock bob?" && log.confirms[0].lines.join(" ").includes("stay open"), "and says sessions stay open");
}
{
  const { ctx, log } = makeCtx({ confirm: false });
  check(await actions.runAction(ctx, "lock", user()) === false && log.sent.length === 0, "declining lock sends nothing");
  check(await actions.runAction(ctx, "ban", user()) === false && log.sent.length === 0, "declining ban sends nothing");
  check(await actions.runAction(ctx, "unlock", user()) === false && log.sent.length === 0, "declining unlock sends nothing");
}
{
  const { ctx, log } = makeCtx();
  await actions.runAction(ctx, "ban", user());
  check(log.typed[0].word === "bob" && log.typed[0].danger === true && sent(log) === JSON.stringify([["/api/users/ban", { name: "bob", confirm: "bob" }]]), "ban needs the name typed and sends it as the confirmation");
  await actions.runAction(ctx, "unlock", user({ expired: true }));
  check(log.sent[1][0] === "/api/users/unlock", "unlock goes to its endpoint");
}
{
  const { ctx, log } = makeCtx({ form: { password: "0123456789", password2: "0123456789", must_change: true } });
  await actions.runAction(ctx, "password", user());
  check(sent(log) === JSON.stringify([["/api/users/password", { name: "bob", password: "0123456789", must_change: true }]]), `password: only what the server wants (the repeat never leaves): ${sent(log)}`);
  check(log.forms[0].fields.some((f) => f.name === "password2" && f.matches === "password" && f.required), "the password form asks twice");
  check(log.forms[0].fields.find((f) => f.name === "password").minLength === 10, "and knows the minimum");
}
{
  const { ctx, log } = makeCtx({ form: { new_name: "robert", rename_home: true } });
  await actions.runAction(ctx, "rename", user());
  check(sent(log) === JSON.stringify([["/api/users/rename", { name: "bob", new_name: "robert", rename_home: true, confirm: "bob" }]]), `rename: ${sent(log)}`);
  check(log.forms[0].typed === "bob" && log.forms[0].danger, "rename needs the name typed");
  check(log.forms[0].fields.some((f) => f.name === "rename_home" && f.value === true), "and offers the home rename when the home is named after the user");
  const odd = actions.renameForm(user({ home: "/srv/people/b" }));
  check(!odd.fields.some((f) => f.name === "rename_home"), "but not when it is not");
  check(!formProblem(odd.fields, { new_name: "ok_name" }) && formProblem(odd.fields, { new_name: "Bad Name" }) !== null, "the new name is checked as the server will");
}
{
  const { ctx, log } = makeCtx({ form: { path: "/srv/b", move: false } });
  await actions.runAction(ctx, "home", user());
  check(sent(log) === JSON.stringify([["/api/users/home", { name: "bob", path: "/srv/b", move: false, confirm: "bob" }]]), `home: ${sent(log)}`);
  check(actions.homeForm(user()).fields.find((f) => f.name === "move").value === true, "moving is the default");
  check(log.forms[0].typed === "bob" && log.forms[0].danger === true, "changing the home folder needs the name typed");
}
{
  const { ctx, log } = makeCtx({ form: { delete_home: true } });
  await actions.runAction(ctx, "remove", user());
  check(sent(log) === JSON.stringify([["/api/users/remove", { name: "bob", delete_home: true, confirm: "bob" }]]), `remove: ${sent(log)}`);
  check(log.forms[0].typed === "bob" && log.forms[0].danger === true, "removing needs the name typed");
  check(actions.removeForm(user()).warning.join(" ").includes("cannot be undone") && actions.removeForm(user()).fields[0].value !== true, "removing warns, and deleting the home is not pre-ticked");
}
{
  const { ctx, log } = makeCtx({ form: null });
  for (const id of ["password", "rename", "home", "remove"]) await actions.runAction(ctx, id, user());
  check(log.sent.length === 0, "cancelling any form sends nothing");
  check(await actions.runAction(ctx, "no-such-action", user()) === false && log.sent.length === 0, "an unknown action does nothing");
}

// ---- the home folder forms use the picker
check(actions.homeForm(user()).fields.find((f) => f.name === "path").type === "folder", "the home folder is a folder field");
check(actions.addForm({}).fields.find((f) => f.name === "home").type === "folder", "so is the home folder when adding a user");
{
  const { ctx, log } = makeCtx({ form: async (o) => ({ path: await o.browse("path", { path: "" }), move: true }) });
  await actions.runAction(ctx, "home", user({ home: "/home/bob" }));
  check(JSON.stringify(log.picks[0]) === JSON.stringify({ start: "/home", name: "bob", forUser: "bob" }), `Browse… starts next to the current home, offers the user name: ${JSON.stringify(log.picks[0])}`);
  check(log.sent[0][1].path === "/mnt/Extra20/chosen", "and the chosen folder is what is sent");
}
{
  const { ctx, log } = makeCtx({ form: async (o) => { await o.browse("path", { path: "/srv/people/bob" }); return null; } });
  await actions.runAction(ctx, "home", user({ home: "/home/bob" }));
  check(log.picks[0].start === "/srv/people", "it starts next to what has been typed, if anything");
}
{
  const { ctx, log } = makeCtx({ form: async (o) => { await o.browse("home", { name: "dave", home: "" }); return null; } });
  await actions.addUser(ctx, { shells: ["/bin/bash"] });
  check(JSON.stringify(log.picks[0]) === JSON.stringify({ start: "", name: "dave", forUser: "" }), `when adding, the typed user name is offered and no account is involved: ${JSON.stringify(log.picks[0])}`);
}

// ---- add user
const DATA = { shells: ["/bin/sh", "/bin/bash"], has_sudo_group: true };
{
  const form = actions.addForm(DATA);
  check(form.fields.find((f) => f.name === "shell").value === "/bin/bash" && form.fields.find((f) => f.name === "shell").options.length === 2, "the shell list comes from the server, bash first");
  check(form.fields.some((f) => f.name === "sudo") && !actions.addForm({ ...DATA, has_sudo_group: false }).fields.some((f) => f.name === "sudo"), "sudo is offered only if there is a sudo group");
  check(actions.addForm({}).fields.find((f) => f.name === "shell").options[0].value === "/bin/bash", "without a list the form still works");
  check(form.fields.find((f) => f.name === "password").required === false, "the password is optional when adding");
  check(formProblem(form.fields, { name: "dave", comment: "a:b" }) !== null && formProblem(form.fields, { name: "dave", comment: "Dave D" }) === null, "the full name is checked");
  check(formProblem(form.fields, { name: "dave", password: "short", password2: "short" }) !== null, "a given password must be long enough");
  const body = actions.addBody({ name: "dave", comment: "", shell: "/bin/sh", home: "", sudo: false, password: "", password2: "", must_change: false });
  check(JSON.stringify(body) === JSON.stringify({ name: "dave", shell: "/bin/sh", comment: "", sudo: false, must_change: false }), `blank optional fields are left out: ${JSON.stringify(body)}`);
  const full = actions.addBody({ name: "dave", comment: "Dave", shell: "/bin/bash", home: "/srv/dave", sudo: true, password: "0123456789", password2: "0123456789", must_change: true });
  check(full.home === "/srv/dave" && full.password === "0123456789" && full.sudo === true && !("password2" in full), "filled fields are sent, the repeat is not");
  const { ctx, log } = makeCtx({ form: { name: "dave", comment: "", shell: "/bin/bash", home: "", sudo: false, password: "", password2: "", must_change: false } });
  check(await actions.addUser(ctx, DATA) === true && log.sent[0][0] === "/api/users/add", "adding sends to the add endpoint");
  const cancelled = makeCtx({ form: null });
  check(await actions.addUser(cancelled.ctx, DATA) === false && cancelled.log.sent.length === 0, "cancelling the add form sends nothing");
}

// ---- ending sessions
{
  const { ctx, log } = makeCtx();
  await actions.endSession(ctx, { id: "77", user: "bob", from: "10.1.1.1", tty: "pts/2" });
  check(sent(log) === JSON.stringify([["/api/users/end-session", { id: "77" }]]) && log.confirms[0].danger && log.confirms[0].lines.join(" ").includes("bob, from 10.1.1.1 on pts/2"), "ending a session names who and where");
  await actions.endDashboardSignIn(ctx, { id: "aabbccddeeff", ip: "100.1.1.1" });
  check(log.sent[1][0] === "/api/users/end-dashboard" && log.sent[1][1].id === "aabbccddeeff", "ending a dashboard sign-in");
  const no = makeCtx({ confirm: false });
  await actions.endSession(no.ctx, { id: "77", user: "bob" });
  await actions.endDashboardSignIn(no.ctx, { id: "aabbccddeeff" });
  check(no.log.sent.length === 0, "declining either sends nothing");
}

if (failed) { console.log(`${failed} check(s) failed`); process.exit(1); }
console.log("forms smoke ok");
