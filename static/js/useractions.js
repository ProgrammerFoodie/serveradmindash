// The buttons of the Users tab and what each asks before anything is sent. The server decides what is allowed; this only
// decides what is offered and makes sure the person has understood. Forms are plain data so they can be tested.

const NAME_PATTERN = "[a-z_][a-z0-9_-]{0,31}";
const NAME_MESSAGE = "A user name is 1 to 32 characters: lower-case letters, digits, _ and -, starting with a letter or _.";
export const MIN_PASSWORD = 10;

const passwordFields = (label = "New password", required = true) => [
  { name: "password", label, type: "password", required, minLength: MIN_PASSWORD, maxLength: 1024, autocomplete: "new-password",
    help: `At least ${MIN_PASSWORD} characters.` },
  { name: "password2", label: "Repeat the password", type: "password", required, matches: "password", autocomplete: "new-password" },
];

export const isLocked = (u) => !!u.expired || u.password === "locked";

/** Which buttons an account gets: [{id, label, danger}]. root may only get a new password; system accounts get nothing. */
export function accountActions(u) {
  if (u.type === "root") return [{ id: "password", label: "Change password" }];
  if (u.type !== "login") return [];
  return [
    { id: "password", label: "Change password" },
    ...(isLocked(u) ? [{ id: "unlock", label: "Unlock" }] : [{ id: "lock", label: "Lock" }, { id: "ban", label: "Ban…", danger: true }]),
    { id: "rename", label: "Rename…" },
    { id: "home", label: "Change home folder…" },
    { id: "remove", label: "Remove…", danger: true },
  ];
}

export function passwordForm(u) {
  return { title: `New password for ${u.name}`, submitLabel: "Change password",
    lines: u.type === "root" ? ["This is the password of root. SSH keys keep working either way."] : [],
    fields: [...passwordFields(), { name: "must_change", label: "They must choose a new password at the next login", type: "checkbox" }] };
}

export function renameForm(u) {
  const named = (u.home || "").split("/").pop() === u.name;
  return { title: `Rename ${u.name}`, submitLabel: "Rename", danger: true, typed: u.name,
    lines: ["The login name changes; the user number, files and password stay. Its private group is renamed with it.",
      "It is refused while anything runs as this user or still mentions the name (services, supervisor, sudoers, crontab, sshd settings); the refusal says where."],
    fields: [{ name: "new_name", label: "New name", type: "text", required: true, pattern: NAME_PATTERN, patternMessage: NAME_MESSAGE, maxLength: 32 },
      ...(named ? [{ name: "rename_home", label: `Rename the home folder too (${u.home})`, type: "checkbox", value: true }] : [])] };
}

export function homeForm(u) {
  return { title: `Change the home folder of ${u.name}`, submitLabel: "Change home folder", danger: true, typed: u.name,
    lines: [`Now: ${u.home}`, "Moving needs the user to have no running programs, and the new folder must not exist yet. Without moving, the new folder is created (or an existing folder of theirs is used) and the old one stays where it is."],
    fields: [{ name: "path", label: "New home folder", type: "folder", required: true, maxLength: 200, placeholder: `/home/${u.name}`, help: "Type a path, or Browse… to pick the folder." },
      { name: "move", label: "Move the current contents there", type: "checkbox", value: true }] };
}

export function removeForm(u) {
  return { title: `Remove ${u.name}`, submitLabel: "Remove the account", danger: true, typed: u.name,
    lines: ["The account and its private group are deleted. It is refused while the user has running programs (ban it first) or a service or supervisor program runs as it, and for the last user with full sudo rights."],
    warning: ["This cannot be undone."],
    fields: [{ name: "delete_home", label: `Also delete the home folder (${u.home}) and mail`, type: "checkbox" }] };
}

export function addForm(data) {
  const shells = data.shells && data.shells.length ? data.shells : ["/bin/bash"];
  return { title: "Add a user", submitLabel: "Add user",
    lines: ["Creates the account with a private group and a home folder. Give it a password or an SSH key (Users tab, details) or nobody can log in as it."],
    fields: [
      { name: "name", label: "User name", type: "text", required: true, pattern: NAME_PATTERN, patternMessage: NAME_MESSAGE, maxLength: 32 },
      { name: "comment", label: "Full name (optional)", type: "text", maxLength: 100, pattern: "[^:,\\\\\\n\\r]*", patternMessage: "The full name cannot contain : , \\ or line breaks." },
      { name: "shell", label: "Shell", type: "select", options: shells.map((s) => ({ value: s, label: s })), value: shells.includes("/bin/bash") ? "/bin/bash" : shells[0] },
      { name: "home", label: "Home folder (optional)", type: "folder", maxLength: 200, placeholder: "/home/<name>", help: "Leave empty for /home/<name>. It must not exist yet." },
      ...(data.has_sudo_group ? [{ name: "sudo", label: "Give sudo rights (member of the sudo group)", type: "checkbox" }] : []),
      ...passwordFields("Password (optional)", false),
      { name: "must_change", label: "They must choose a new password at the first login", type: "checkbox" },
    ] };
}

/** Only what the server wants: blank optional fields are left out, the repeat field never leaves the page. */
export function addBody(values) {
  const body = { name: values.name, shell: values.shell, comment: values.comment || "", sudo: !!values.sudo, must_change: !!values.must_change };
  if (values.home) body.home = values.home;
  if (values.password) body.password = values.password;
  return body;
}

const dirname = (path) => (path || "").replace(/\/[^/]*$/, "") || (path ? "/" : "");

/** Browse… for a home folder: start next to what is typed (or next to the current home) and offer the user name as the folder name. */
const browseFor = (ctx, u) => (field, values) => ctx.pickFolder({ start: dirname(values[field] || (u && u.home) || ""), name: u ? u.name : (values.name || ""), forUser: u ? u.name : "" });

const CONFIRMS = {
  lock: (u) => ({ title: `Lock ${u.name}?`, confirmLabel: "Lock",
    lines: [`${u.name} cannot log in again (password or SSH key) until you unlock the account.`, "Sessions that are already open stay open. To end them too, use Ban."] }),
  unlock: (u) => ({ title: `Unlock ${u.name}?`, confirmLabel: "Unlock",
    lines: [`${u.name} can log in again. An expiry date the account had before it was locked is put back.`] }),
};

const BAN = (u) => ({ title: `Ban ${u.name}?`, confirmLabel: "Ban", danger: true, word: u.name,
  lines: [`Locks the account and ends ${u.name}'s sessions right now: the programs started in them stop.`, "Their files and any services that run as them stay. Unlock reverses the lock."] });

/** Run one action for an account. Returns true if something was sent and succeeded. */
export async function runAction(ctx, id, u) {
  const act = (op, body) => ctx.act(`/api/users/${op}`, body);
  if (CONFIRMS[id]) {
    if (!(await ctx.confirm(CONFIRMS[id](u)))) return false;
    return !!(await act(id, { name: u.name }));
  }
  if (id === "ban") {
    if (!(await ctx.confirmTyped(BAN(u)))) return false;
    return !!(await act("ban", { name: u.name, confirm: u.name }));
  }
  const forms = { password: passwordForm, rename: renameForm, home: homeForm, remove: removeForm };
  if (!forms[id]) return false;
  const values = await ctx.askForm({ ...forms[id](u), browse: browseFor(ctx, u) });
  if (!values) return false;
  const body = { name: u.name };
  if (id === "password") Object.assign(body, { password: values.password, must_change: values.must_change });
  else if (id === "rename") Object.assign(body, { new_name: values.new_name, rename_home: !!values.rename_home, confirm: u.name });
  else if (id === "home") Object.assign(body, { path: values.path, move: !!values.move, confirm: u.name });
  else if (id === "remove") Object.assign(body, { delete_home: !!values.delete_home, confirm: u.name });
  return !!(await act(id, body));
}

export async function addUser(ctx, data) {
  const values = await ctx.askForm({ ...addForm(data), browse: browseFor(ctx, null) });
  return values ? !!(await ctx.act("/api/users/add", addBody(values))) : false;
}

export async function endSession(ctx, s) {
  const ok = await ctx.confirm({ title: `End the session of ${s.user}?`, confirmLabel: "End session", danger: true,
    lines: [`${s.user}, from ${s.from || "this machine"}${s.tty ? ` on ${s.tty}` : ""}, is signed out at once and the programs started in it stop.`] });
  return ok ? !!(await ctx.act("/api/users/end-session", { id: s.id })) : false;
}

export async function endDashboardSignIn(ctx, d) {
  const ok = await ctx.confirm({ title: "Sign that browser out?", confirmLabel: "Sign out", danger: true,
    lines: [`The dashboard sign-in from ${d.ip || "?"} ends now; whoever uses it has to enter the password again.`] });
  return ok ? !!(await ctx.act("/api/users/end-dashboard", { id: d.id })) : false;
}
