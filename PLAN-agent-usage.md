# Plan: "Agents" tab (sub-agent usage) — phase 20

Adds a read-only **Agents** tab: per sub-agent type, tokens and hours per day for the last 30 days, plus a table and the latest runs.
Replaces the standalone prototype in `/home/claude/agent-usage/` (port 9200), whose logic (`read_run`, `scan`, `report`) is reused.

Status: **built and tested 2026-10-07** (465 tests); needs `deploy/install-admin.sh service` as root, then a look in the browser. Prototype in /home/claude/agent-usage still running/undeleted. Step 4 (import-agents) skipped: the first scan re-reads the transcripts. Written so Sonnet can execute it without design decisions.

## Decisions (already made, do not reopen)

| Topic | Decision |
|---|---|
| Where | New tab `agents`, last in the tab bar. Always on: read-only, no `admin` switch. |
| Source | Claude Code sub-agent transcripts `*/.claude/projects/**/subagents/agent-*.jsonl` + `.meta.json` (agent type, description). The service runs as root, so scan `/home/*/.claude/projects` and `/root/.claude/projects`. Never a user-supplied path. |
| Storage | Own SQLite `DATA_DIR/agents.db` (one row per run, upsert by agent id). NOT `history.db` (that is tiered numeric metrics). Rows outlive transcript cleanup; prune rows older than 400 days. |
| Collector | `dashboard/collectors/agents.py`, cadence `medium` (60 s), incremental: re-parse a file only when its mtime changed. Returns the whole 30-day report, so `/api/live?tab=agents` needs no new endpoint. |
| Tokens | input + output + cache_read + cache_creation, per message id, **max per field** (streamed chunks repeat one message id). Also report "non-cache" = input + output. |
| Time | Wall-clock span first→last message of a run, split across local midnights. Tokens and run count go to the run's start day. |
| Chart | Stacked daily bars by agent type, Tokens/Hours toggle, hover tooltip. A small new `static/js/agentchart.js` (existing `LineChart` is metric/history-based and does not fit). |
| Alerts | None. |
| Standalone prototype | Import its `usage.db` once, then stop it. Deleting `/home/claude/agent-usage/` only after the user confirms (see verify-before-deleting). |

## Steps

### 1. Collector — `dashboard/collectors/agents.py`
- Copy `read_run`, `parse_ts` from `/home/claude/agent-usage/server.py` unchanged. Add: skip files > 50 MB; skip non-regular files (`os.path.isfile` and not symlink).
- `class Agents: cadence = "medium"`; `__init__(self, roots=None, db_path=None)` defaults: roots = `glob("/home/*/.claude/projects") + ["/root/.claude/projects"]` (evaluated at each `collect`, so new users appear), db = `config.DATA_DIR / "agents.db"`. Constructor args exist for tests.
- Schema identical to the prototype's `runs` table plus an index on `end`. Open one connection per `collect` (the scheduler runs it on its background thread only).
- `collect(cfg)`: scan (upsert changed files, prune > 400 days), then build the report with the prototype's `report()` logic, using the host's local timezone. Return:
  `{"days": ["YYYY-MM-DD"×30], "agents": {type: {runs, tokens[30], output[30], seconds[30], runs_per_day[30]}}, "recent": [20 newest runs: {agent, descr(≤120 chars), model, start, seconds, tokens}], "totals": {runs, tokens, seconds}, "scanned": n_files}`.
- No transcript text is ever returned except the ≤120-char `descr` from `.meta.json`.
- Register in `collectors/__init__.py`: `"agents": agents.Agents()` in the medium group; add `agents` to the import line.

### 2. Server — `dashboard/server.py`
- `TABS["agents"] = ["agents"]` (line ~44). Nothing else: `/api/live` already serves any collector listed there.
- Check `dashboard/alerts.py` / `history.extract_metrics` ignore unknown sections (they iterate known names); if either breaks on `agents`, guard it.

### 3. Page
- `static/js/agentchart.js`: `export class AgentChart` with `update({days, agents}, mode)`; DOM columns (flex, one per day, stacked segments), colours from the existing palette variables in `static/app.css` (find the series colours used by `chart.js`; do not invent hex values). **CSP check**: `server.py` sends a strict `Content-Security-Policy`; set segment heights via `element.style.height = …` (CSSOM, allowed) never via a `style="…"` attribute or inline `<style>`. Tooltip as in the prototype but built with `el()`.
- `static/js/tabs/agents.js`: `export default {id: "agents", title: "Agents", create(ctx) {…}}` following `tabs/logs.js`: `panel()`, `dataTable()` (columns Agent, Runs, Tokens, Non-cache, Time, Per active day; sortable, default sort tokens desc), segmented Tokens/Hours buttons (`el("div",{class:"seg"})`), a "Recent runs" table, "Waiting for data…" / error states exactly like `logs.js`'s `wait()`. Use `fmtNum`, `fmtBytes`-style helpers from `util.js`; add `fmtTokens` (k/M) there if missing. Remember the user's ad blocker: no class/id names `banner` or `ad*`.
- `static/js/app.js`: `import agents from "./tabs/agents.js"`; add to `BASE_TABS`.
- Empty state: "No sub-agent runs in the last 30 days." (true today: the newest of the 7 known runs is from 2026-09-06).

### 4. Import the prototype's history (one-off)
- Add subcommand `python3 -m dashboard import-agents <path-to-usage.db>` in `dashboard/__main__.py`: `INSERT OR IGNORE` all rows into `agents.db`. Not needed if the transcripts are still on disk (the first scan re-reads them), so keep it tiny; skip it if `__main__.py` has no natural place.

### 5. Tests (standard library only; keep `python3 -m unittest` green, currently 455)
- `tests/test_collectors.py` (or new `tests/test_agents.py`): temp dir with fake `projects/x/<sid>/subagents/agent-1.jsonl` + `.meta.json`; assert (a) repeated message id counted once with max values, (b) token sum, (c) a run crossing midnight splits seconds across two days but counts once, (d) unchanged mtime is not re-parsed (monkeypatch `read_run` counter), (e) a changed/grown file is re-parsed, (f) malformed JSON lines and missing `.meta.json` → agent `unknown`, no crash, (g) rows older than 400 days pruned, (h) empty roots → empty report, (i) output has no keys other than the documented ones.
- `tests/test_server.py`: `/api/live?tab=agents` returns `sections.agents`; unknown tab still 400.
- `tests/test_frontend.py` / `tests/js`: follow how the Logs tab is covered; add a render test for an empty and a populated payload.

### 6. Verify (before reporting done)
```bash
cd /mnt/Extra20/admin && python3 -m unittest 2>&1 | tail -3
python3 -m dashboard check agents        # if `check` takes collector names (it does for `users`); else call Agents().collect({}) in a REPL
```
Then, **as root**, `deploy/install-admin.sh service` (restarts the service; I cannot do this). Open the Agents tab in the user's Chrome (override `document.hidden` when driving it from the browser tool). Create one real run to see data (ask Claude to launch an Explore sub-agent), wait ≤ 60 s, check numbers against `sqlite3 data/agents.db`.

### 7. Wrap-up
- Stop the prototype: `pkill -f /home/claude/agent-usage/server.py`. Ask the user before deleting `/home/claude/agent-usage/`.
- Update README.md (tab list) and the status block at the top of this file; commit (no push without asking; no real domains/IPs/paths of devices in commits).

## Risks / edge cases
- **Memory**: service is capped at 192 MB. Parsing is line-streamed and incremental; the first scan is the only large one (a few hundred transcripts at most). Verify `systemctl status` memory after the first scan.
- **Growing files**: a running sub-agent's transcript changes mtime each message → re-parsed each minute until it ends; fine.
- **Timezone**: the host's local zone defines "day"; the browser may differ. Show the zone in the panel subtitle.
- **Cleanup**: Claude Code deletes old transcripts; the SQLite rows keep history, but only for runs seen while the dashboard was running.
- **Sensitivity**: `descr` is the task title of the user's own runs, shown only to the authenticated user. Do not log it.
- Out of scope (offer later): cost estimate per model, per-project breakdown, main-session (non-sub-agent) usage.

## Review follow-up (2026-10-07)
Two Opus code reviews are in `reviews/code-reviewer/`. Fixed in the commit after phase 20 (497 tests):
- agents.py: files opened with O_NOFOLLOW/O_NONBLOCK + fstat checks and size caps (meta 64 KB, line 1 MB, file 50 MB); a fixed three-level
  scandir walk with no symlinked directories; work budget per minute; rows keyed by full path; unusable files remembered; DST-correct days.
- security.py SSH regexes (forged "from"), logs.py journal `-n` limit and per-entry error handling, util.pressure without PSI, network operstate race.
- Front end: keyboard-operable table rows that keep focus on refresh, Agents chart not rebuilt for unchanged data, visible focus ring.
- Backend: Lock/Ban now really block an account whose password was already locked (expiry is what stops SSH keys), Unlock leaves a prior
  `passwd -l` and any real expiry alone; home parents must belong to root and not be writable by others; a failed add never removes an
  account it did not create; private files are written with `safefs.write_private` / O_NOFOLLOW.
Not done (needs root or a decision): H1 root-owned deploy of code/config/data (`chown -R root:root`, then the owner edits as root), background
jobs for slow home moves/removals (M2), leftover sudoers/crontab/sshd rules on removal (M3 is reported as notes today), L2-L5 backend items.
Timezone of the host is UTC, so the DST finding was latent; "Time" is wall-clock span (first to last message).
