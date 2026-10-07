# Plan: admin tools (phases 13-19)

Adds to the dashboard: **reboot and "restart all services"**, **users and active sessions** with account management,
**SSH keys** (view, add, remove, reveal private keys) and an **editable config viewer**. No web terminal: the dashboard stays terminal-less.

Status: **phase 13 done** (2026-10-07); phases 14-19 not started. Decisions below were made by the owner on 2026-10-07.

### Phase 13: what was built, and how it differs from the text below
- `config.json` `admin` block with the five switches (`dashboard/config.py`: `ADMIN_SWITCHES`, `admin_switches()`). Missing means off,
  only a real `true` turns a tool on, anything else in the block is a config error. `App.require_feature(name)` answers 404 for a
  tool that is off. `/api/session` reports `admin: {...}`. Later phases add their own keys (`restart_order`, `configs`) to the validator.
- `dashboard/safefs.py`: `open_in_home`, `read_in_home`, `ensure_dir`, `atomic_write`, `backup`, `list_backups`. Refuses symlinks,
  hard links, FIFOs, files of other users and group/world-writable folders. `ensure_dir` is an addition: phase 17 needs it to create `~/.ssh`.
- `dashboard/jobs.py` plus `Actions.run_job(kind, body, who, work)`: a job holds the one-action-at-a-time lock until it ends and is
  audited and announced like any action. `GET /api/jobs/<id>` (session only; ids are 16 hex characters).
- `Actions.register(kind, handler, label)`: later phases add their actions through this, so they inherit the lock, rate limit, audit
  entry and Telegram message. Also `actions.clean()` (printable one-liners) and `actions.require_confirmation(body, expected)`
  (the server-side check of a typed word; a wrong or missing word is a 400).
- Telegram/audit: no new code was needed; `register` and `run_job` go through the existing `_report`.
- Page: `static/js/confirm.js` (`ctx.confirm` as before, plus `ctx.confirmTyped({word, ...})`: the confirm button stays disabled until
  the word is typed, Enter cannot confirm a wrong word, Cancel keeps the focus in the plain variant).
- Service unit: `ProtectHome=no` and `ReadWritePaths=/mnt/Extra20/admin/data /etc /home /root -/var/spool/cron -/var/mail`
  (a test pins this list). `install-admin.sh service` now **restarts** the service (it used `enable --now`, which does not restart a
  running service, so new unit settings would never have applied).
- To apply on the server (as root): `cd /mnt/Extra20/admin && deploy/install-admin.sh service`. Phase 13 has no visible feature;
  add the `admin` block to the real `config.json` when the first admin tool exists.

## Decisions

| Topic | Decision |
|---|---|
| Restart | Reboot the server (typed confirmation, optional 1-minute countdown that can be cancelled) **and** "restart all services" |
| Sign-in | Password only, as today. No authenticator code, no general re-confirmation |
| Users | Add, remove, ban/unban, lock/unlock, change password, rename, change home folder, end sessions |
| Ban | Lock the account **and** end its sessions. Files and running programs stay. Reversible |
| Protected accounts | Only login users (UID 1000+) can be renamed, removed, banned, locked or moved. root's password and keys can be changed. The last sudo-capable user can never be removed, banned or locked |
| SSH keys | View authorized keys and host keys; add and remove authorized keys; list and reveal private keys |
| Configs | Editable, with diff preview, backup, syntax check before applying, automatic undo on failure |
| Terminal | None |

**What this means for security, stated once:** with these features, a signed-in dashboard session can do almost anything
root can (create a sudo user, add an SSH key, edit sshd_config). The protections are: Tailscale-only access, the password,
the 15-minute inactivity sign-out, typed confirmations, the audit log, a Telegram message for every admin action, and
per-feature switches in the root-only `config.json` that the web page itself can never turn on.

Revealing a private key is the one place that asks for the password again (it was part of the chosen option);
drop that requirement if it gets in the way.

## Before starting

1. Commit the current uncommitted work (inactivity sign-out, Overview redesign) and restart the service once.
2. Read `PLAN.md` sections on actions (phase 7) and the security model: the new features follow the same patterns
   (allow-lists, argv lists never through a shell, one action at a time, audit + Telegram, CSRF on every POST).

## Phase 13 - Foundation

**Feature switches.** New optional block in `config.json` (validated in `config.py`; missing means **off**):

```json
"admin": { "power": true, "users": true, "ssh_keys": true, "private_keys": true, "configs": true }
```

A disabled feature's endpoints return 404 and its UI is hidden (`/api/session` reports which are on).
`config.example.json` gets the block with every switch on; the owner adds it to the real `config.json` as root.

**Service sandbox** (`deploy/server-dashboard.service`). Today `ProtectHome=yes` hides `/home` and `/root`, and
`ProtectSystem=strict` makes `/etc` read-only. Change to:

```ini
ProtectHome=no
ProtectSystem=strict
ReadWritePaths=/mnt/Extra20/admin/data /etc /home /root /var/spool/cron /var/mail
```

`/usr`, `/boot` and the rest of `/var` stay read-only. `NoNewPrivileges=yes` stays (everything runs as root already).
`MemoryMax=192M` stays.

**`dashboard/safefs.py`** - the only code that touches files in places users can write to:
- `open_in_home(user, relpath)`: walks each path component with `os.open(..., O_NOFOLLOW | O_DIRECTORY, dir_fd=...)`;
  refuses symlinks, anything not owned by that user or root, group/world-writable directories, and files with
  `st_nlink > 1` (a hard link to `/etc/shadow` must never be read or written).
- `atomic_write(dir_fd, name, data, uid, gid, mode)`: temp file in the same directory, `fchown`, `fchmod`, `fsync`,
  `os.rename(..., src_dir_fd=, dst_dir_fd=)`.
- `backup(path)`: copy into `data/backups/<path with / replaced>/<UTC timestamp>` (mode 600, last 20 kept).

**`dashboard/jobs.py`** - long operations (restart all, config apply with auto-undo) run as a background job:
`{id, kind, started_by, steps: [{label, state, detail}], state, started, finished}` held in memory; `GET /api/jobs/<id>`;
one job at a time (shares the existing `Actions` lock); the UI polls it every second while open.

**Audit and Telegram.** Every admin action is written to `data/audit.jsonl` (allowed or refused) and sent to Telegram
as one line, e.g. `👤 user dashtest added by <you> from <your Tailscale address>`. Secrets (passwords, key material, config contents)
never appear in either.

**UI.** One shared confirm dialog variant with "type X to confirm" (`ctx.confirmTyped(title, body, word)`).

## Phase 14 - Power

**Reboot** - `POST /api/power/reboot {confirm, delay}`:
- `confirm` must equal the hostname; `delay` is `0` or `60` seconds.
- Runs `shutdown -r +1 "Reboot from the admin dashboard by <user>"` (or `shutdown -r now`).
- Before running: writes `data/reboot_pending.json` `{by, ip, requested, boot_id}` (boot id from
  `/proc/sys/kernel/random/boot_id`) and sends Telegram `🔁 reboot in 60 s, requested by ...`.
- `POST /api/power/cancel` runs `shutdown -c` and sends Telegram.
- The scheduled state comes from logind: `busctl get-property org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager ScheduledShutdown`.
  While a reboot is scheduled, every tab shows a banner (like the alert bar) with the countdown and a Cancel button.
- On start-up, if `reboot_pending.json` exists and the boot id changed: Telegram `✅ back up after N s`, audit entry,
  delete the file. Same boot id and older than 10 minutes: stale, delete.

**Restart all services** - `POST /api/power/restart-all {confirm}` (`confirm` = hostname), runs as a job:
1. Units from `watch.systemd` minus exclusions, in this order: data stores (`redis-server`), then app services and
   `supervisor`, then supporting services (`cron`, `chrony`, `fail2ban`, `smbd`, `ssh`), then `nginx` last.
   Order and exclusions are overridable in `config.json` (`admin.restart_order`).
2. Always excluded: `server-dashboard` and `admin-dns` (would kill the job itself), `tailscaled` (would cut the only way in).
   Listed in the confirm dialog so it is clear what is not restarted.
3. Each unit: `systemctl restart -- <unit>`, then wait up to 30 s for `is-active`. A failure is recorded and the job
   continues with the next unit. Final Telegram: `restart all: 11 ok, 1 failed (smbd)`.

UI: a "Power" card at the top of the Services tab with both buttons.

## Phase 15 - Users and sessions (read-only part)

New collector `users` (slow cadence, 60 s; refreshed immediately after any user action via `scheduler.touch`):

| Field | Source |
|---|---|
| name, uid, gid, comment, home, shell | `/etc/passwd` |
| groups, primary group | `/etc/group` |
| password state: set / none / locked; last change; expiry; account expired | `/etc/shadow` (only these derived values; **hashes never leave the parser**) |
| sudo-capable | member of `sudo` or `admin`, **or** named in a file in `/etc/sudoers.d/` or `/etc/sudoers` (simple name match; `%group` lines resolved through `/etc/group`) |
| last login (time, from, tty) | `/var/log/lastlog` (292-byte records indexed by uid: int32 time, 32-byte line, 256-byte host) |
| login-capable | shell is not `nologin`/`false`, account not expired/locked, and has a password or at least one key |
| home exists, home owner/mode | `stat` |
| number of authorized keys | phase 17 parser |
| running processes | count from the processes collector |
| type | `login` (uid ≥ 1000, < 65534) / `root` / `system` |

Sessions: the existing `loginctl` data plus idle state (`IdleHint`, `IdleSinceHint`), service (`sshd`, `login`),
remote host, TTY, leader PID and process count. Dashboard sessions from `data/auth.db`: id (first 12 hex characters of the
token hash, never the token), IP, browser, created, last activity, "this is you".

UI: new **Users** tab: "Signed in now" (system sessions and dashboard sessions), "Users" table (login users and root;
"show system accounts" toggle), and a per-user dialog with all details. Badges: sudo, locked, expired, no password, no keys.

## Phase 16 - User actions

All in `dashboard/users.py`, through `Actions.perform` (lock, rate limit, audit, Telegram), argv lists only, `--` before
every user name. Passwords go to `chpasswd` on **stdin only** (never argv, never logged), minimum 10 characters.

| Action | Command(s) | Refused when |
|---|---|---|
| Add user | `useradd -m -d HOME -s SHELL -c COMMENT [-G sudo] -- NAME`, then optional `chpasswd`, optional first key (phase 17) | name taken or not `^[a-z_][a-z0-9_-]{0,31}$`; shell not in `/etc/shells`; comment has `:` or newline; bad home (see below) |
| Change password | `chpasswd` (stdin `name:password`); option "must change at next login": `chage -d 0 -- NAME` | (allowed for root and login users) |
| Lock | `usermod -L -e 1 -- NAME` (`-e 1` is required: a password lock alone does not stop SSH keys); `-L` skipped if the account has no password | protected account; last sudo-capable user |
| Unlock | `usermod -e '' -- NAME`, plus `usermod -U` only if a real hash is behind the `!` | |
| Ban | Lock, then `loginctl terminate-user -- NAME` | as Lock |
| Unban | Unlock | |
| End session | `loginctl terminate-session -- ID` (system), or revoke row in `auth.db` (dashboard) | unknown id |
| Rename | `usermod -l NEW -- OLD`; if the private group has the same name `groupmod -n NEW -- OLD`; option "rename home too": `usermod -d /home/NEW -m -- NEW` | protected; user has running processes; referenced by a watched unit (`systemctl show -p User -p Group`), a supervisor program (`user=`) or a sudoers file - the refusal lists where, so it can be fixed first |
| Change home | `usermod -d NEWDIR [-m] -- NAME` (`-m` = move contents); if not moving and the folder does not exist: create it (mode 750, owned by the user) | protected; processes running (when moving); path not absolute, contains `..`, or is or is inside `/`, `/bin`, `/boot`, `/dev`, `/etc`, `/lib*`, `/proc`, `/root`, `/run`, `/sbin`, `/sys`, `/usr`, `/var` (except `/var/www`); parent missing |
| Remove | `userdel -- NAME`; option "also delete home folder": `userdel -r -- NAME` | protected; last sudo-capable user; running processes (ban first); referenced by a watched unit or supervisor program |

Typed confirmations: remove, rename, ban and change home require typing the user name.

## Phase 17 - SSH keys

**Read** (`dashboard/sshkeys.py`):
- Where keys live: `sshd -T` (as root) gives `authorizedkeysfile` (expand `%h`, `%u`, `%%`), `passwordauthentication`,
  `permitrootlogin`, `pubkeyauthentication`. These also feed a small "SSH settings" summary with warnings
  (e.g. root login with password allowed).
- Parse each user's authorized keys through `safefs`: options (quoted strings may contain commas), key type, base64 blob,
  comment. Unparseable lines are kept and shown as "unrecognised line N", never dropped on rewrite.
- Fingerprint: `SHA256:` + base64(sha256(blob)) without padding (same as `ssh-keygen -lf`); RSA bit length from the blob.
- "Last used": map fingerprints to the newest `Accepted publickey for USER from IP ... SHA256:...` line in `auth.log`.
- Host key fingerprints from `/etc/ssh/ssh_host_*_key.pub`, shown for checking a first connection.

**Add / remove** (`POST /api/ssh/keys/add {user, key}`, `/remove {user, fingerprint}`):
- Add: one line, parsed by our parser **and** checked with `ssh-keygen -lf /dev/stdin`; refuse anything containing
  `PRIVATE KEY` with a clear message ("that is a private key; paste the .pub file"); refuse duplicates (same fingerprint).
  Creates `~/.ssh` (700) if missing; writes with `safefs.atomic_write` (600, owned by the user). Confirmation shows the fingerprint.
- Remove: by fingerprint; rewrites the file without that line, keeping everything else byte for byte.
  If it is the user's last key and the account has no password (or password login is disabled in sshd), the dialog warns
  that the user will no longer be able to log in, and requires typing the user name.

**Private keys** (feature switch `private_keys`):
- List: regular files (opened with `O_NOFOLLOW`, under 16 KB) in each user's `~/.ssh` and `/root/.ssh` whose first line is a
  `-----BEGIN ... PRIVATE KEY-----` header. Shown: path, owner, mode (warning if not 600), type, fingerprint (from the
  matching `.pub`, or `ssh-keygen -y -f` if unencrypted), and whether it is passphrase-protected (OpenSSH format: cipher name
  inside the key is `none` or not).
- Reveal: `POST /api/ssh/private/reveal {path, password}`; path must be one from the list; password checked with the same
  scrypt verify and failure limiter as sign-in. Response has `Cache-Control: no-store`. UI: dialog with the key and a Copy
  button, cleared after 60 seconds and on close. Audit + Telegram `🔑 private key <path> revealed by <user> from <ip>`.

UI: in the per-user dialog of the Users tab ("SSH keys" section with Add/Remove), plus cards "Server host keys",
"Private keys on this server" and "SSH settings".

## Phase 18 - Config editor

**Allow-list** (in code, overridable in `config.json` `admin.configs`), grouped, each with a check command and an apply command:

| Group | Files | Check (must pass before applying) | Apply | Auto-undo |
|---|---|---|---|---|
| nginx | `nginx.conf`, `sites-available/*`, `conf.d/*.conf`, `snippets/*.conf` | `nginx -t` | `systemctl reload nginx` | yes, 60 s |
| SSH server | `sshd_config` (check `sshd -t -f <candidate>` before writing), `sshd_config.d/*.conf` | `sshd -t` | `systemctl reload ssh` | yes, 120 s |
| fail2ban | `jail.local`, `jail.d/*` | `fail2ban-client -t` | `fail2ban-client reload` | no |
| supervisor | `conf.d/*.conf` | parse as INI | `supervisorctl reread` then `update` | no |
| systemd units | regular files in `/etc/systemd/system/*.service` (not symlinks) | `systemd-analyze verify` | `systemctl daemon-reload` (restart is a separate, explicit button) | no |
| cron | `/etc/crontab`, `/etc/cron.d/*`; user crontabs via `crontab -u USER` | field count per line; `crontab` validates user crontabs itself | none needed | no |
| Samba | `smb.conf` | `testparm -s <candidate>` | `systemctl reload smbd` | no |
| chrony | `chrony.conf` | `chronyd -p -f <candidate>` | `systemctl restart chrony` | no |
| firewall | `nftables.conf` | `nft -c -f <candidate>` | `nft -f /etc/nftables.conf` | yes, 60 s |
| sudo | files in `/etc/sudoers.d/` (`/etc/sudoers` itself read-only) | `visudo -c -f <candidate>` | none needed | no |
| mounts | `/etc/fstab` | `findmnt --verify --tab-file <candidate>` | none (takes effect at next boot); typed confirmation because a bad fstab can stop the server from booting | no |
| hosts | `/etc/hosts` | none | none | no |
| certificate renewal | `/etc/letsencrypt/renewal/*.conf` | - | **view only** | - |

Never editable: `/etc/shadow`, `/etc/gshadow`, `/etc/passwd`, `/etc/group` (managed through the Users tab), the
dashboard's own `config.json`, and anything outside the allow-list. Paths are resolved (`sites-enabled` symlinks lead to
the real file) and the resolved path must itself be in the list.

**Save flow** (`POST /api/configs/save {path, sha256, content}`, runs as a job, one at a time):
1. Refuse if the file changed since it was loaded (`sha256` mismatch, 409), the content is over 512 KB, not UTF-8, or contains NUL.
2. Show the unified diff (`difflib`) and ask for confirmation; `fstab` and the dashboard's own nginx site and unit need typing the file name.
3. Backup with `safefs.backup`; write atomically, keeping owner and mode.
4. Run the check. On failure: restore the backup byte for byte, show the checker's output, stop.
5. Run the apply command. On failure: restore, re-apply, show the output.
6. Auto-undo groups: the page must call `POST /api/configs/confirm {job}` within the window, **through nginx** (which proves
   the dashboard is still reachable). Without confirmation the backup is restored and re-applied, and Telegram says so.
   For SSH, the dialog tells you to open a new SSH connection before confirming.
7. Audit entry with the diff stored under `data/backups` (mode 600); Telegram gets only the path and the line counts
   (`📝 /etc/nginx/sites-available/x edited: +3 -1`).

**Backups**: each file shows its last 20 backups with date and author; "restore" goes through the same save flow.

UI: new **Configs** tab: grouped file list on the left (on phones a select), viewer with line numbers, Edit (plain
monospace textarea, Tab inserts a tab), "Review changes" (diff), Save, and the job progress with the auto-undo countdown.

## Phase 19 - Docs, install, verification

- README: the new tabs, the `admin` switches, how to turn a feature off.
- `PLAN.md`: summary of phases 13-19 with lessons learned.
- Install: `deploy/install-admin.sh service` (new unit), then add the `admin` block to `config.json`, restart.
  No nginx change is needed (no WebSocket, no new public paths).

## Tests

Standard library `unittest` plus the Node scripts, as now. No test needs root or changes the real system:
- Parsers with fixture files: `passwd`, `shadow` (assert no hash ever appears in output), `group`, sudoers files,
  binary `lastlog` built in the test, `authorized_keys` (options with quoted commas, comments with spaces, bad lines),
  `sshd -T` output.
- Fingerprints and private-key detection against keys generated in a temp folder with `ssh-keygen` (with and without
  passphrase); compare our fingerprints with `ssh-keygen -lf`.
- `safefs` attacks in a temp tree: symlinked `.ssh`, symlinked `authorized_keys`, hard link, group-writable folder,
  file owned by someone else - all refused.
- Every user, power and config operation with a fake command runner: exact argv, password only on stdin, every refusal
  rule (root, system account, last sudo user, running processes, referenced by a unit).
- Config flow: check fails -> file restored byte for byte; apply fails -> restored; no confirmation -> auto-undo;
  stale `sha256` -> 409; path outside the list or escaping through a symlink -> 403.
- HTTP: each endpoint needs a session and CSRF; returns 404 when its switch is off; rate limit applies.
- Front end: smoke tests for the Users and Configs tabs in the fake DOM (like `tests/js/overview_smoke.mjs`).

**Manual checks after install** (owner, as root, with a throwaway user `dashtest`): add user with a key and log in;
change password; lock (login refused) and unlock; ban (session ends, login refused) and unban; rename with home; move home;
add and remove a key; reveal a throwaway private key (Telegram arrives); edit a comment in an nginx snippet (applies,
confirm); break nginx syntax on purpose (refused, file unchanged); save without confirming (auto-undo after 60 s);
restart all; schedule a reboot and cancel it; reboot for real (Telegram "back up" message); remove `dashtest`.

## Order and size

Phases are done in order; each ends with all tests passing and is usable on its own.
Rough size, judged against the existing code: 13 small, 14 medium, 15 medium, 16 large, 17 medium, 18 large, 19 small -
about as much new code as the whole dashboard has today.
