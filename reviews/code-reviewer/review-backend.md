# Backend security review: server admin dashboard (part 1, backend)

- Scope: /mnt/Extra20/admin as it is on disk (not a git repository, so there is no diff: every file in scope was read in full).
  Files: dashboard/useradmin.py, safefs.py, jobs.py, config.py, alertmanager.py, alerts.py, scheduler.py, history.py, audit.py,
  __main__.py, dnsd/admin_dns.py, deploy/* (nginx templates, systemd units, install-admin.sh, render.py). server.py, actions.py,
  util.py and collectors/users.py were read only where they interact with these files. tests/ were checked for gaps.
- Date: 2026-10-07
- Reviewer: code-reviewer subagent (read-only; nothing was changed)

## Summary

- Stack: Python 3.14, standard library only, runs as root under systemd (ProtectSystem=strict with /etc, /home, /root and
  /mnt/Extra20 writable), behind nginx with a Tailscale-IP allow-list, SQLite for history and sessions.
- Standards used: the project's own rules as written in module docstrings, PLAN/README and the tests (argv lists with `--`,
  passwords only on stdin, no secrets in audit/Telegram, "cannot check is not fine", nothing followed through symlinks as root).
- Verdict: **Changes Requested** (2 High findings). No Critical finding.
- Overview: the code is careful and consistent. Argument injection, SQL injection, path traversal in the folder browser and
  secret leakage are handled well. The real risks are at the edges: the code and data that root runs are owned by an
  unprivileged account (H1), Lock/Ban silently does nothing for one common account state (H2), and the home-folder checks
  stop at the folder itself and do not look at who controls its parent (M1).

Severity scale used: Critical / High (both count as Blocking) and Medium / Low / Nit (Non-Blocking).

## Pre-checks

- Focus: OK. The reviewed files form one coherent feature set.
- Leftovers: OK. No debug prints, commented-out code or temporary comments found in the files in scope.
- PR context: Unverified. Not a git repository, so there are no commit messages or PR description; the purpose was taken from
  the module docstrings and the caller's brief.
- Automation: Tests verified: `python3 -B -m unittest discover -s tests` ran 465 tests, all OK (56 s, a few ResourceWarnings
  in test_actions.py). No linter or SAST configuration or CI config found, so lint/SAST status is unverified.
- Secrets and dependencies: OK in code (no hardcoded tokens; config.json is root 0600; deploy/local.env and allowed-devices.txt
  are 0600). No third-party dependencies. File ownership around those secrets is an issue, see H1.

## Blocking (Must Fix)

### H1 (High). Root runs code, config and data that the unprivileged user `claude` (uid 1001) controls

- Location: whole tree. Evidence (`ls -l`): `/mnt/Extra20/admin` is `drwxr-xr-x claude claude`, `dashboard/` is `drwxrwxr-x claude
  claude`, every `dashboard/*.py` is `-rw-rw-r-- claude claude`, `data/` is `drwx------ claude claude` (files inside are root),
  `deploy/install-admin.sh` and `render.py` are owned by claude. `deploy/server-dashboard.service:9-12` runs
  `/usr/bin/python3 -m dashboard serve` as `User=root` with `WorkingDirectory=/mnt/Extra20/admin`.
- Problem / exploit paths (any process running as uid 1001, for example an agent session or a compromised npm/pip
  install in that account, without knowing any sudo password):
  1. Edit any `dashboard/*.py` (or drop a `json.py` next to it: `-m` puts the working directory first on sys.path); the next
     restart of `server-dashboard` (Restart=on-failure, reboots, the "service" install stage) runs it as root.
  2. Because the directory is claude-owned, claude can rename the root-owned `config.json` away and put its own in place
     (own password hash, `admin.users=true`): the dashboard then accepts claude's password for root-level actions.
  3. `data/` is claude-owned, so claude can plant symlinks that root later opens with following:
     `useradmin.py:134-138` (`LockLedger._save` opens `data/locks.tmp` with `O_CREAT|O_TRUNC`, no `O_NOFOLLOW`/`O_EXCL`):
     a symlink `data/locks.tmp -> /etc/shadow` makes the next Lock truncate /etc/shadow and write JSON into it (then
     `os.replace` moves the symlink, but the damage is done). `history.py:102,110` (`os.open(..., O_CREAT)` then `os.chmod`)
     and `audit.py:33` (`O_APPEND|O_CREAT`) follow symlinks the same way.
  4. `install-admin.sh` is run with sudo and executes `render.py` from the same claude-writable folder.
  The sandbox does not help: `ReadWritePaths=/mnt/Extra20` (`server-dashboard.service:31`) gives the service write access to
  its own code too.
- Why it matters: uid 1001 is effectively root through the dashboard, and silently (no sudo log, no audit entry). If uid 1001
  is meant to need a sudo password, this bypasses it.
- Fix direction: install the running copy root-owned and not group/other-writable (for example `chown -R root:root` a
  deployed copy such as `/opt/server-dashboard` or `/usr/local/lib/...`, mode 755/644, and point WorkingDirectory/ExecStart
  there; keep the claude-owned tree as the development checkout only). Make `data/` and the config root-owned 0700/0600.
  Independently, open private files with `O_NOFOLLOW` (`LockLedger._save`: `O_EXCL|O_NOFOLLOW` plus a random tmp name, as
  `config.save` already does with mkstemp). Optionally set `ReadOnlyPaths=` for the code directory in the unit.

### H2 (High). Lock and Ban do nothing for an account whose password is already locked but which has SSH keys

- Location: `dashboard/useradmin.py:344-346` (`_is_locked`), used by `_lock` (`:551-552`) and `_lock_account` (`:535-536`).
- Problem: `_is_locked` returns True when `acct["password"] == "locked"` (a `!`-prefixed hash, for example after `passwd -l`
  or `usermod -L` done outside the dashboard). For such an account:
  - Lock is refused with 409 "already locked".
  - Ban calls `_lock_account`, which returns "already locked" without running `usermod -e 1`, then ends the sessions and
    reports "bob is banned (already locked); its sessions are ended".
  But a `!` password does not stop SSH public-key logins; the module's own docstring (`:120`) says the expiry is what stops
  keys, and the users collector agrees (`collectors/users.py:424`: "locked" without keys is a blocker, with keys it can log in).
  So the banned user reconnects with their key seconds later while the dashboard and audit log say "banned".
- Test gap: `tests/test_useradmin.py:303-307` asserts this behaviour (`{"password": "locked"}` -> 409) instead of catching it.
- Fix direction: treat "locked" as `bool(acct["expired"])` only (the state that blocks every login method). For Lock/Ban on an
  account with a `!` password but no expiry, still run `usermod -e 1` (without `-L`) and record the ledger. Add tests for
  "password locked, has keys, not expired" on Lock and Ban.

## Non-Blocking / Suggestions

### M1 (Medium). Home folders are accepted under parents that another user owns or can write

- Location: `useradmin.py:456-462` (`_parent_problem`), used by `_add` (`:472`) and `_home` (`:683`); `home_problem` (`:53-81`)
  is string-only.
- Problem: the parent is only checked for existence and for being symlink-free. Its owner and mode are not. Scenario: the admin
  creates `deploy` (sudo=true) with home `/mnt/Extra20/projects/deploy`, where `projects` is owned by bob (or is 1777). bob
  renames `deploy` away and puts his own folder there (or does it before `useradd -m`, which then just uses the existing folder),
  containing `.bashrc`/`.profile`. sshd's StrictModes refuses key logins for that home, but password logins still run bob's
  `.bashrc` as `deploy`, a sudoer, so bob can capture the sudo password. The same parent control lets bob swap the home for a
  symlink to a root-owned folder later; `safefs.open_in_home` (`safefs.py:74`) opens `home` with `O_NOFOLLOW` only on the last
  component and `_owned` accepts root-owned folders, so a future writing tool (ssh_keys) could be redirected (today only
  `read_in_home` is used, so that part is latent).
- Fix direction: in `_parent_problem` (and as a `selectable` reason in `browse`), require every ancestor of the home to be owned
  by root (or the account itself) and not group/other-writable (the sshd StrictModes rule), walking with `O_NOFOLLOW` dir fds.
  Apply the same ancestor check in `safefs.open_in_home` before trusting `home`.

### M2 (Medium). Long `usermod -m` / `userdel -r` run with a 60 s kill timeout inside a 30 s nginx request

- Location: `useradmin.py:42` (`COMMAND_TIMEOUT_S = 60`), `:666`, `:699`/`:711`, `:759`; `util.py:37` (`subprocess.run(...,
  timeout=...)` kills the child on timeout); `deploy/nginx-admin-proxy.conf:13` (`proxy_read_timeout 30s`).
- Problem: moving a home from /home to /mnt/Extra20 (the main use case in the unit comment) crosses filesystems, so `usermod -m`
  copies. With a few GB this takes more than 60 s: usermod is SIGKILLed mid-copy after /etc/passwd already points at the new path
  (`:392` docstring). `_after_failed_home_change` then sees `new_there` and reports "the account now uses NEW, and OLD is still
  there", leaving the user on a half-copied home. `userdel -r` on a large home behaves the same way (account gone, home partly
  deleted, message at `:434` says "was not deleted"). Before 60 s, nginx already returns 504 after 30 s, so the browser shows a
  timeout while the action keeps running.
- Fix direction: run home moves and removals as Jobs (`Actions.run_job` exists) without a kill timeout (or a much larger one),
  and on failure of a move after the passwd change, point the account back to the old home and remove the partial copy only if
  it was created by this run. Correct the "was not deleted" message to "may be partly deleted".

### M3 (Medium). Removing an account leaves name-based grants that a later account with the same name inherits

- Location: `useradmin.py:746-748` with `strict=False` (`:372-375`): sudoers mentions, crontab and sshd AllowUsers are only
  "notes" on remove; `_new_name` (`:439-446`) checks only existing users and groups.
- Problem: remove `alice` who had `alice ALL=(ALL) NOPASSWD: ALL` in /etc/sudoers.d; months later Add a different person as
  `alice`: they silently get full root. Same for `/var/spool/cron/crontabs/alice` (cron runs it as the new alice) and the
  LockLedger entry (`:148` keeps the stale expiry, Unlock restores it).
- Fix direction: on Add, run `Dependents.find` for the new name and refuse (or require an explicit acknowledgement) when sudoers,
  a crontab or sshd rules already name it. Optionally make sudoers mentions a blocker for Remove. Drop any ledger entry for a
  name on Add.

### L1 (Low). `_after_failed_add` can delete an account it did not create

- Location: `useradmin.py:416-427`.
- Problem: if `useradd` fails because the name appeared between the snapshot and the command (created over SSH, exit 9 "user
  already exists"), `_fresh_account` finds that account and `userdel` removes it. Narrow race, but the code assumes any account
  of that name is the half-created one.
- Fix direction: only roll back when the exit code is not 9 and the fresh account's uid was not present before, or compare uids.

### L2 (Low). Service sandbox is minimal

- Location: `deploy/server-dashboard.service:20-31`.
- Problem: only NoNewPrivileges, PrivateTmp, ProtectSystem are set. With `/etc` writable and systemctl/loginctl reaching pid 1
  over D-Bus, the sandbox cannot contain a compromise, but cheap extra settings narrow accidents and the kernel attack surface.
- Fix direction: consider `ProtectKernelModules=yes`, `ProtectKernelTunables=yes`, `ProtectControlGroups=yes`,
  `ProtectClock=yes`, `RestrictSUIDSGID=yes`, `LockPersonality=yes`, `RestrictNamespaces=yes`, `RestrictRealtime=yes`,
  `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK`, `SystemCallArchitectures=native`, and `ReadOnlyPaths=` for the
  code directory (see H1). Test each against the actions before enabling.

### L3 (Low). Login lockout can be triggered by any local process

- Location: `server.py:127-135` trusts `X-Real-IP` from any loopback peer; `auth.py:166` overall limit of 30 failures per 15 min.
- Problem: the overall limit is the right defence against forged addresses, but it means any local user can keep the owner
  locked out by posting wrong passwords directly to 127.0.0.1:9100 (nginx's limit_req is bypassed), and the audit/session
  "ip" fields can be forged the same way. Accepted by design per the comment at `auth.py:162`; listed so it is a conscious choice.
- Fix direction: optionally listen on a Unix socket with mode 0600/root-only group shared with nginx (`proxy_pass
  http://unix:...`), which removes both the forgery and the bypass.

### L4 (Low). Unbounded growth of history metric ids

- Location: `history.py:125-131`, `:176`, `:198`; `collectors/network.py:22-47` records every interface except `lo`.
- Problem: the `metrics` table is never pruned and maintenance loops over every id ever seen (2 rollups + 3 deletes per id per
  minute, under the history lock). Interface churn (container veths, if containers are ever run) adds ids forever, slowly making
  the per-minute maintenance heavier on 1 vCPU. Unverified whether containers run on this host.
- Fix direction: delete metric ids that have no rows in any tier during daily maintenance, or skip veth/docker/br- interfaces.

### L5 (Low). Folder browser lists a whole directory into memory

- Location: `useradmin.py:838-860`.
- Problem: `list(os.scandir(path))` plus a `home_problem` loop over all users per entry, before truncating to 500. A folder with
  millions of entries can push the service past `MemoryMax=192M` (OOM kill, then restart). Admin-only, so Low.
- Fix direction: stop iterating after a cap (for example 5000 entries) and report "truncated".

### N1 (Nit). Allow-list entries are not checked to be Tailscale addresses

- Location: `deploy/render.py:49-66`.
- Problem: any IP is accepted, so a typo (a public or LAN address) widens access silently.
- Fix direction: warn or refuse when an address is outside 100.64.0.0/10 or fd7a:115c:a1e0::/48.

### N2 (Nit). `agents.db` is 0644

- Location: `data/agents.db` (created by collectors/agents.py, outside this part's files). Not reachable by others today
  because `data/` is 0700, but every other data file is forced to 0600.

## Questions for the Author

- AQ1. Is uid 1001 (`claude`) meant to be root-equivalent without a password (sudo NOPASSWD)? If yes, H1 drops to Medium
  (defence in depth only); if no, H1 stays High. Could change the verdict only together with H2.
- AQ2. Does /etc/nginx/nginx.conf (not read: out of bounds for this review) set `real_ip_header`/`set_real_ip_from`
  globally? If it does, `$remote_addr` in the `geo` allow-list (`nginx-admin.conf.template:6-9`) could be taken from a client
  header and the allow-list bypassed. Unverified; please confirm it is not set. Could raise severity to Critical if it is.

## Checked and found sound (no finding)

- Argument injection: every useradd/usermod/groupmod/userdel/chage/loginctl/systemctl call is an argv list with `--` before the
  name; names match strict regexes; home paths must start with `/`; shell must be listed in /etc/shells; comment rejects `:,\`
  and control characters.
- Passwords: only on chpasswd stdin; control characters and newlines refused; redacted from error text twice
  (`util.py:45-46`, `useradmin.py:362-363`); audit/Telegram labels never include them (`useradmin.py:303-315`).
- Folder browser: plain-path and realpath checks, symlinks never entered, system roots and other homes refused; `current_id` for
  end_dashboard is set by the server (`server.py:410`).
- SQL: all values bound as parameters; interpolated table/column names are constants (`history.py`).
- Concurrency: one action at a time via a non-blocking lock (`actions.py:156`), so user-admin TOCTOU between two requests is
  not possible; `LockLedger`, `Audit`, `History`, `AlertManager`, `Jobs` all lock their shared state.
- Telegram: HTML escaped; token never placed in error text; queue bounded (50).
- DNS responder: answers one name only, never answers responses, no recursion, bounded TCP slots and packet sizes, runs as a
  DynamicUser with only CAP_NET_BIND_SERVICE.
- nginx: allow-list enforced at server level on 443 (also for /login), port 80 only redirects allowed devices and serves the
  ACME path, X-Real-IP/X-Forwarded-For overwritten, body limit 16k, login rate limit.

## Pillar Coverage

- Logical Soundness: Concern (H2, M2, L1).
- Extensibility: OK (actions registry, Jobs, safefs are good extension points; M1 matters before the ssh_keys writer lands).
- Blast Radius: Concern (H1, M1, M3, L3, L4, L5).
- Test Quality: Concern. 465 tests pass and coverage of refusals is broad, but a test enshrines H2
  (`tests/test_useradmin.py:303-307`), and there are no tests for parent ownership (M1), command timeouts (M2), or name reuse (M3).
- Documentation: OK. Docstrings explain the safety rules clearly; update the useradmin docstring once H2/M1 change the rules.

## Merge and Post-Merge Checklist

- Re-run the full test suite after the fixes; add the tests listed under H2, M1, M2, M3.
- Redeploy from a root-owned copy (H1), then verify: `stat` shows root ownership of code, config.json and data/; the service
  starts; `/healthz` answers.
- Manual check for H2 on a test account: `passwd -l test`, keep a key in `~test/.ssh/authorized_keys`, Ban it from the
  dashboard, confirm `chage -l test` shows an expiry in 1970 and a key login fails.
- Watch after deployment: `journalctl -u server-dashboard` for "could not put the home folder", "could not remove the
  half-created account" and "audit log" errors; data/audit.jsonl for failed users.* actions; memory of the service
  (`systemctl status server-dashboard`, MemoryMax 192M) while browsing large folders; nginx admin.access.log for 504 on
  POST /api/users/*.
