# Code review, part 2: front end and collectors (server admin dashboard)

- Date: 2026-10-07
- Scope: /mnt/Extra20/admin at commit 1b103c6 ("Phase 20: Agents tab"), whole tree, read-only.
  Files: static/index.html, static/login.html, static/app.css, static/login.css, static/login.js, static/js/*.js,
  static/js/tabs/*.js, dashboard/collectors/*.py; tests/js/*.mjs, tests/test_collectors.py, tests/test_agents.py,
  tests/test_frontend.py. Backend files (dashboard/util.py, dashboard/scheduler.py) were read only where needed to judge
  the blast radius of a collector.
- Extra scrutiny: dashboard/collectors/agents.py, static/js/agentchart.js, static/js/tabs/agents.js (new today).

## Summary

- Stack and standards: Python 3.14 stdlib backend run as root (systemd, MemoryMax=192M, 1 vCPU / ~1 GB VPS); vanilla
  ES-module front end under CSP `default-src 'self'`. Standards taken from the project itself: the util.js header
  ("text always goes in as text nodes, never as HTML"), tests/test_frontend.py (no innerHTML, no inline style/script),
  the collector isolation contract in dashboard/collectors/__init__.py, and the "only agent type and title leave this
  module" contract in agents.py. No linter config was found for JS or Python.
- Verdict: **Changes Requested** (2 High findings, both in the new agents collector).
- Overview: the front end is in good shape for XSS: every string reaches the DOM as a text node, no innerHTML, no URL
  attributes built from data, styles only through CSSOM. Polling, hidden-tab handling, 401 handling and the tab-switch
  guard are correct. The weak spot is the new agents collector: as root it walks and opens files inside user-owned home
  directories without symlink/file-type/size protection, on the scheduler thread that every alert depends on. Secondary
  issues: SSH log parsing that lets an attacker forge the "From" address, unbounded journalctl output under a log storm,
  agent time accounting that counts idle gaps, keyboard access for clickable rows, and thin tests for the parsers.

Severity mapping used below: Critical/High = Blocking; Medium/Low/Nit = Non-Blocking.

## Pre-checks

| Check | Status |
|---|---|
| Focus | OK. Commit 1b103c6 is the Agents tab only (collector, chart, tab, tests, README/plan). |
| Leftovers | OK. No debug prints/console.log leftovers (console.warn/error are deliberate error paths); no commented-out code seen. |
| PR context | OK. PLAN-agent-usage.md and the commit message describe purpose and approach. |
| Automation | Unverified. No CI config in the repo. I did not run `python3 -m unittest` or the node smoke tests (reviewer rules: no commands beyond read-only git without the owner choosing them explicitly). Run `python3 -m unittest discover -s tests` and `for f in tests/js/*_smoke.mjs tests/js/helpers.mjs; do node $f; done` before merging. |
| Secrets and dependencies | OK. No secrets in the reviewed files; no third-party dependencies. Password hashes do not leave users.py (only `password_state`). Process command lines are shown in full up to 400 chars, see L13. |

## Blocking (Must Fix)

### H1 (High). agents.py opens files inside user-owned trees as root with no symlink, file-type or size check
- Location: dashboard/collectors/agents.py:61-63 (`.meta.json`), :39 (transcript), :97-101 (check before open).
- Problem: `read_run` opens `agent-<id>.meta.json` with a plain `open()` + `json.load()`. Nothing checks that it is a
  regular file, not a symlink, or small. The transcript itself is checked with `os.lstat`/`islink` (:98-101), but then
  opened by path afterwards (a check-then-use race) and read without an upper bound (the 50 MB cap is from the stat,
  not enforced while reading).
- Failure scenario: any account that owns a `/home/<u>/.claude/projects` tree (the `claude` account that runs agents,
  or anything running as it) creates `.../subagents/agent-x.jsonl` with one valid line and
  `agent-x.meta.json -> /dev/zero` (or a FIFO). Within 60 s the root collector reads /dev/zero until the cgroup hits
  MemoryMax=192M and the service is OOM-killed (restart loop, since the file stays), or blocks forever in `open()` on
  the FIFO. The agents collector runs on the single shared "background" thread (dashboard/scheduler.py:98-116), so a
  hang also stops services, fail2ban, ssh_auth, journal, nginx, updates, ssl and the history flush: every alert goes
  silent with no error shown.
- Fix direction: open with `os.open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)`, `os.fstat` the fd, require
  `S_ISREG`, re-check the size on the fd, and read at most N bytes (meta: e.g. 64 KB; transcript: MAX_FILE_BYTES).
  Add tests for a FIFO meta, a /dev/zero symlink meta, and a transcript swapped for a symlink.

### H2 (High). Recursive glob as root follows symlinked directories in user-owned trees
- Location: dashboard/collectors/agents.py:31, :96.
- Problem: `glob.glob(root/*/**/subagents/agent-*.jsonl, recursive=True)`. Python's `**` also matches symbolic links
  to directories (glob documentation), and `/home/*/.claude/projects` is writable by its owner. The `islink` check at
  :101 covers only the last path component.
- Failure scenario: (a) loop: a user creates `projects/p/a -> .` and `projects/p/b -> .`. Each extra level doubles the
  paths (a/a, a/b, b/a, ...) until the kernel's 40-symlink limit: about 2^40 directory listings. The background
  thread spins on the single vCPU indefinitely, with the same "all alerts silent" effect as H1. This can also happen by
  accident (a tool that creates a `current -> .` link). (b) reach: a symlinked directory such as `p/x -> /root/...`
  lets root-only `agent-*.jsonl`/`.meta.json` files of other trees be counted and their 120-character task titles
  shown. The impact is small, but it crosses a privilege boundary.
- Fix direction: the layout is fixed (`<projects>/<project>/<session>/subagents/agent-*.jsonl`, agents.py:3), so
  `**` is not needed. Walk exactly three levels with `os.scandir` and `entry.is_dir(follow_symlinks=False)` /
  `entry.is_file(follow_symlinks=False)`. Optionally skip a home tree whose root (`/home/u/.claude`) is a symlink or
  not owned by that user's uid. Add tests with a symlink loop and a symlinked project directory.

## Non-Blocking / Suggestions

### Medium

**M1. agents.py: memory and CPU cost of re-reading transcripts**
- Location: dashboard/collectors/agents.py:39-55, :104-106, :92-113.
- Problem: (a) every line is `json.loads`-ed whole. A single 50 MB line (a big tool result or base64 image, or a
  crafted file) becomes a 50 MB str, or up to 200 MB if it has one non-BMP character, because CPython then stores the
  whole string at 4 bytes per char. Add the parsed dict and it exceeds MemoryMax=192M. (b) "Incremental" means
  "whole file again when mtime changes": an active sub-agent's transcript changes every minute, so it is fully
  re-parsed every 60 s. (c) There is no per-cycle budget: the first scan after deploy, or after the DB is lost, parses
  every transcript of every user in one `collect()` call on the shared background thread.
- Fix direction: read with `f.readline(LIMIT)` and skip (drain) over-long lines. Only lines with `"usage"` need
  `json.loads`; take timestamps from a cheap regex on the line prefix. Cap work per cycle (for example 200 files or
  100 MB, continue next minute). Optionally store a byte offset plus partial aggregates per file for a truly
  incremental read.
- Benefit: bounded memory under the 192M cap; the background thread stays responsive.

**M2. SSH auth parsing lets an attacker forge the source address shown**
- Location: dashboard/collectors/security.py:118-120.
- Problem: `Invalid user (\S*) from (\S+) port` and `Failed (\S+) for (?:invalid user )?(\S+) from (\S+) port` take
  the first " from X port" in the line. SSH user names may contain spaces; OpenSSH logs them unescaped (from my
  knowledge of OpenSSH source, not checked on this host).
- Failure scenario: user name `x from 203.0.113.9 port 1` produces
  `Invalid user x from 203.0.113.9 port 1 from 198.51.100.7 port 5555`. The dashboard counts 203.0.113.9 in "Most
  active addresses" and "Recent failed attempts". The attacker hides its own address and can point at any IP the
  admin might then ban. For `Failed ... for invalid user ` with an empty name the line is not counted at all.
- Fix direction: anchor at the end and match greedily:
  `^Invalid user (.*) from (\S+) port \d+(?: \S+)*$` and
  `^Failed (\S+) for (?:invalid user )?(.*) from (\S+) port \d+`, using the greedy `(.*)` so the last " from" wins.
  Truncate stored user names (e.g. 64 chars). Add parser tests with these lines.

**M3. journalctl output after the cursor is unbounded (memory under a log storm)**
- Location: dashboard/collectors/logs.py:49-57 (only the first call has `-n20000`); dashboard/util.py:37 (`capture_output`).
- Failure scenario: a crash loop or a kernel/network warning storm writes 100k+ priority-warning entries in a minute.
  `run()` holds the whole JSON output (~1 KB per entry, so 100 MB+) in memory, then `splitlines()` copies it. That is
  well past MemoryMax=192M, and the scheduler budget already allows ~80 MB for `apt list` (scheduler.py:20). The same
  family, at smaller scale: LogTail backfills up to 8 MB from `.1` plus 8 MB from the current file per tailer
  (util.py:94-113; two tailers: auth.log, nginx) and decodes it into a list of str. The SshAuth deque holds up to
  100k tuples with attacker-length user names (security.py:128-132).
- Fix direction: add `-n{MAX_ENTRIES}` to the cursor call as well (losing the middle of a storm is acceptable, and
  journal counts can be noted as "at least"). Or stream with `Popen` and stop after N lines. Measure peak RSS of all
  collectors together once, with backfilled logs, against the 192M limit.

**M4. Agent "time" counts idle gaps; tokens are attributed to the start day only**
- Location: dashboard/collectors/agents.py:46-47, :136-144.
- Problem: run time is `last timestamp - first timestamp`. A sub-agent that waits on a permission prompt, or is
  resumed hours or days later, gets the whole gap counted as agent time, split across those days. All of its tokens
  land on the start day even if most were spent later. "Hours" can therefore show a day of activity for an idle
  agent, and the token and hour charts disagree about which day work happened.
- Fix direction: sum the gaps between consecutive message timestamps, capping each gap (e.g. at 5 min), and bucket
  both seconds and tokens per day by message timestamp (store per-day aggregates per run). At least state in the UI
  that "Time" is wall-clock span and is summed across parallel agents (it can exceed 24 h per day).

**M5. Day boundaries use a fixed UTC offset (DST)**
- Location: dashboard/collectors/agents.py:126-130.
- Problem: `datetime.fromtimestamp(now).astimezone().tzinfo` is a fixed-offset timezone for *now*. On a host with a
  DST zone, every midnight before the last DST change is off by one hour for 30 days. The next EU change is
  2026-10-25, inside the window. If the host runs in UTC this does not apply; I did not check the host timezone.
  The test (tests/test_agents.py:62-70) uses the same construction, so it cannot catch this.
- Fix direction: build naive local midnights (`datetime.fromtimestamp(now).replace(hour=0, ...)` and naive
  `+ timedelta(days=i)`). Calling `.timestamp()` on a naive datetime uses the local zone rules, including DST, per day.
  Add a test with `TZ=Europe/Riga` and `time.tzset()`.

**M6. Agents tab rebuilds the chart every 5 s; keyboard focus and the detail line are lost**
- Location: static/js/tabs/agents.js:44-58, static/js/agentchart.js:30-42.
- Problem: `tick()` polls the visible tab every 5 s (app.js:21, :167-184), and `update()` calls `chart.setData()`
  every time, which `replaceChildren`s all 30 day columns (tabindex=0) with new listeners. The data changes at most
  once a minute.
- Failure scenario: a keyboard or screen-reader user tabs onto a day column to read its numbers. Within 5 s the
  focused node is removed, focus falls back to `<body>`, and the tab order restarts at the top of the page.
- Fix direction: skip the rebuild when the section timestamp (`s.t`) has not changed, or keep the bars and update
  only heights and labels. Restore focus to the same day index if a rebuild is needed.

**M7. Clickable table rows are not reachable by keyboard**
- Location: static/js/util.js:203-210 (`onRowClick` adds only a click listener); used by
  static/js/tabs/processes.js:52 (Terminate / Force kill) and static/js/tabs/users.js:48 (every account action).
- Problem: `<tr>` has no tabindex, role or key handler, so with keyboard only these actions can't be reached at all.
- Fix direction: give clickable rows `tabindex="0"` and an Enter/Space keydown handler, or render a real
  `<button>` in the first cell (better for screen readers).

**M8. Test gaps in collectors and front-end logic**
- Location: tests/test_collectors.py (35 lines: only `firewall_block` and `parse_readonly`); tests/test_agents.py;
  tests/js/*.
- Not covered: rate and percentage maths (`cpu._split`, DiskIO busy/await, NetIO rates, the cgroup CPU %);
  `SshAuth._parse` (see M2), `Nginx._ingest`, `Journal.collect` (cursor, PRIORITY handling), LogTail rotation,
  truncation and partial lines; `_hex_ip` for IPv6/IPv4-mapped; `read_wtmp`; `unit_of`. For agents: the adversarial
  cases from H1/H2/M1, DST (M5), runs that started before the window (seconds counted, run not counted), and
  `recent` ordering. Front end: the app.js tab-switch guard and the 401 path. The agents smoke test does not exercise
  the error-then-recovery path or focus behaviour.
- Fix direction: these parsers are pure functions of text, so table-driven unit tests with captured sample lines are
  cheap. Add them together with the M2/M3 fixes.

### Low

**L1.** agents.py:69, :93, :104. The run key is the file basename only. Two transcripts with the same `agent-<id>`
name in different projects or homes overwrite each other's row, and each scan re-reads both because the mtimes
differ. A local user can overwrite root's rows on purpose. Fix: key by `(root, relative path)` or a hash of the full
path.

**L2.** agents.py:106-110. A transcript with no usable timestamp returns None and is never stored, so it is re-opened
and parsed every 60 s forever. Fix: store a tombstone (mtime only).

**L3.** agents.py:50. `m.get("id") or d.get("uuid")` can be None for both, which merges all such messages into one
entry with max-per-field and undercounts tokens. Fix: fall back to a per-line counter.

**L4.** static/js/tabs/agents.js:46-47. On "waiting" or error only the chart panel changes; "By agent type" and
"Latest runs" keep stale rows with no hint. The number of agent types is unbounded (user-controlled `agentType`):
colours repeat after 6 (agentchart.js:6) and the payload grows. Fix: blank or annotate all three panels; group types
beyond the top ~10 into "other".

**L5.** static/js/tabs/security.js:126-132. After one `history_error`, the "Login history" panel is replaced by the
error text, and `hist.set(history.el)` is never called again, so it stays an error until reload even after recovery.
Fix: `hist.set(history.el)` on each successful update, as is done for `now`.

**L6.** static/js/tabs/overview.js:161-173, :245-262; static/js/tabs/network.js:74-79. Disk IO, network and throughput
charts are built once from the first device/interface list; later interfaces (Docker, WireGuard) never get a series.
Unmounted filesystems keep their row and card with the last values forever. Fix: rebuild when the name set changes;
mark or remove vanished mounts.

**L7.** dashboard/collectors/security.py:55-58 (also users.py:347). `read_wtmp` reads the whole file every 60 s to keep
the newest 40 records. Fix: seek to the last `limit * k * 384` bytes; scan backwards only as far as needed.

**L8.** dashboard/collectors/disks.py:15, :40. Only `\040` is decoded from /proc/mounts and mountinfo; `\011`, `\012`
and `\134` are not. Such a mount point gets a wrong path, an error from `statvfs`, and a read-only map lookup that
misses. Fix: decode with `re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), s)`.

**L9.** dashboard/collectors/network.py:47. Reading `/sys/class/net/<if>/operstate` for an interface that disappeared
between the two reads raises, and the whole net_io sample fails (and its history point is lost). Fix: catch OSError
per interface.

**L10.** dashboard/util.py:51-58 (used by cpu.py:55, memory.py:76, disks.py:132). If PSI is not available
(`/proc/pressure` missing, kernel booted without PSI), the whole cpu, memory and disk_io collectors fail, not just
the pressure fields. Fix: return `{}` on OSError; the front end already handles a missing `some` (usage.js:57).

**L11.** dashboard/collectors/logs.py:64-67. A non-integer PRIORITY raises ValueError after `_cursor` has advanced, so
the rest of that batch is silently dropped. logs.py:222: a certificate without `notAfter` raises AttributeError and
fails the whole SSL section. Fix: per-entry/per-cert try/except.

**L12.** static/js/app.js:167-184. No request timeout: a request that hangs (half-open Tailscale path) stops polling
until the browser gives up, possibly minutes. The status dot does show "no connection" after 15 s. A late response
from an earlier tick for the same tab (A to B to A quickly) can overwrite newer data. Fix: `AbortController` with
~10 s timeout; a sequence number per tick.

**L13.** dashboard/collectors/processes.py:100, static/js/tabs/processes.js:36, :65. Full command lines (400 chars)
are shown, so secrets passed on argv (`mysql -p...`, tokens) go to the browser and into the DOM. This matches what
`ps` shows to local users, so it is acceptable for an admin-only tool, but consider masking `--password=`,
`-p<x>`, `token=` patterns.

**L14.** static/js/power.js:68, :106. `follow()` can run twice for the same job (page-load pickup plus "already
running" click), which polls twice per second. Fix: keep the followed job id and do not start a second loop.

### Nit

**N1.** static/login.js:7. `NOTES[why]` with `?why=constructor` prints the Object constructor's source text. Not
exploitable (textContent). Use `Object.hasOwn(NOTES, why)`.

**N2.** static/app.css:253. `.agent-day:focus-visible { outline: none }` leaves only a subtle background change as the
focus indicator; keep an outline.

**N3.** static/js/tabs/overview.js:72. Card ids built from mount points can collide (`/a-b` and `/a/b` both give
`ov-disk-a-b`), so `aria-controls` points at the wrong card.

**N4.** dashboard/collectors/processes.py:27. `PF_KTHREAD` is defined but unused; kernel-thread detection uses
`ppid == 2`. Use the flag (field 9 is already parsed into `flags`).

## Questions for the Author

**AQ1.** dashboard/collectors/agents.py:31. Is scanning *every* `/home/*` tree intended, or only the accounts that run
Claude Code (config list)? A config allowlist would shrink the H1/H2 attack surface. This does not change the
verdict (H1/H2 must be fixed either way).

**AQ2.** Host timezone: is the VPS on UTC? If yes, M5 is latent only; if it is Europe/Riga or similar, M5 shows up
from 2026-10-25. This does not change the verdict.

**AQ3.** M4: is "Time" meant to be wall-clock span (including waits and resumes) or active working time? The answer
decides whether M4 is a bug or a labelling issue.

## Pillar Coverage

- Logical Soundness: Concern. M2, M4, M5, L1-L3, L11.
- Extensibility: OK. The collector registry and dataTable/panel helpers are easy to extend; L6 is the main rigidity.
- Blast Radius: Concern. H1, H2, M1, M3 (shared background thread, 192M cap, root reading user-owned paths).
- Test Quality: Concern. M8 (parsers and rate maths untested; adversarial agents cases missing).
- Documentation: OK. Module docstrings and comments are clear. README was updated for the Agents tab in the same
  commit (not re-checked line by line).

Verified as OK (no finding): no `innerHTML`, `insertAdjacentHTML` or `outerHTML` anywhere (enforced by
tests/test_frontend.py:55-56). `el()` puts every child in as a text node and sets `on*` handlers only from code-defined
attribute keys. No `href`/`src` built from data. Styles only through CSSOM (bar widths, chart segment heights, tooltip
position). Polling skips hidden tabs and resumes on `visibilitychange`. Responses for a tab that is no longer active
are discarded (app.js:174). A 401 anywhere redirects once (api.js:12). Idle sign-out does not count polling as
activity. Password fields are cleared when the form dialog closes. Rate maths in cpu/memory/disks/network/processes
check out: elapsed from monotonic clock, counter reset returns 0, busy% = io_ms/elapsed/10, cpu% = ticks/CLK_TCK per
core. `/proc/net/tcp6` word order is handled. `ss` lines with `%iface` are handled. Subprocesses all have timeouts
(5 s default, 15-60 s where needed).

## Merge and Post-Merge Checklist

1. Fix H1 and H2, add their tests, rebase on the current main, then run the full Python suite and every
   tests/js/*.mjs (CI does not exist, so do it by hand) and record the result in the commit message.
2. Merge per project convention (direct commits on main, one commit per phase, as the history shows).
3. After deploy, watch:
   - `systemctl status server-dashboard`, plus `journalctl -u server-dashboard` for "oom-kill" / "Killed" and for
     restarts (`NRestarts`).
   - Peak memory: `systemctl show -p MemoryPeak,MemoryCurrent server-dashboard` over the first hour (the first agents
     scan reads every transcript) and over a day with apt list.
   - That medium collectors keep updating: Services/Security tabs "updated" timestamps (section `t`) stay under
     ~60-70 s old. A stale section means the background thread is blocked.
   - Agents tab: compare one day's totals against a hand count from a few transcripts; check the days after
     2026-10-25 if the host is not on UTC.
   - auth.log brute-force bursts: "Most active addresses" should match `grep "Invalid user" /var/log/auth.log` tail.
