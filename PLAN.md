# Server Admin Dashboard: Build Plan

**Goal:** https://admin.example.com is a dark, tabbed, very detailed live dashboard for this server. It is reachable **only over Tailscale** (phone and laptop) and protected by a username and password. It keeps 90 days of history, can control services and kill processes, and sends alerts to Telegram.

Written 2026-10-06 from a survey of this machine. Steps marked **[root]** need `sudo` (the `claude` user's sudo asks for a password). Every other step runs as `claude`.

---

## 1. How it fits together

```
 Phone / laptop (Tailscale on)
   │ 1. "admin.example.com?" ──► Tailscale Split DNS ──► admin-dns (100.64.0.1:53)
   │                                                     answers 100.64.0.1
   │ 2. HTTPS to 100.64.0.1:443 (inside WireGuard)
   ▼
 nginx  (server_name admin.example.com)
   ├─ source IP in 100.64.0.0/10 or fd7a:115c:a1e0::/48 → proxy to 127.0.0.1:9100
   └─ anything else (public internet)                    → 444 (connection closed)
   ▼
 server-dashboard (Python stdlib, root, 127.0.0.1:9100)
   ├─ collectors  → /proc, /sys, systemctl, journalctl, fail2ban-client, supervisorctl, logs
   ├─ history     → SQLite: 5s (24h) → 1min (7d) → 15min (90d)
   ├─ alerts      → dashboard banner + Telegram Bot API (urllib)
   └─ actions     → systemctl / supervisorctl / kill (allow-listed, confirmed, audit-logged)

 Internet → admin.example.com → public IP 203.0.113.10
   :80  only /.well-known/acme-challenge/ (Let's Encrypt renewals), everything else 444
   :443 444
```

**Why nginx doesn't bind to the Tailscale IP:** nginx starts before `tailscaled` at boot. If nginx were set to `listen 100.64.0.1:443`, it would fail to start, and **all your sites (app1, app3, example.com) would go down with it**. Instead, nginx listens on all addresses as it already does, and the admin server block only allows Tailscale source IPs. The DNS responder uses `IP_FREEBIND`, so it can also start before Tailscale.

---

## 2. Inventory: what's already here, what's needed

### Already installed and used as-is (nothing to install)
| Component | Version | Used for |
|---|---|---|
| Python | 3.14.4 | the whole app; stdlib only |
| ↳ `sqlite3` | SQLite 3.46.1 | history database |
| ↳ `ssl`, `urllib.request` | OpenSSL 3.5.5 | Telegram API over HTTPS |
| ↳ `hashlib.scrypt`, `hmac`, `secrets` | | password hashing, sessions, CSRF |
| ↳ `http.server` (ThreadingHTTPServer) | | web server behind nginx |
| ↳ `socket`, `struct` | | DNS responder; parsing `/var/log/wtmp` |
| systemd / journalctl | 259 | services, the journal, cgroup v2 per-service CPU and RAM |
| nginx | 1.28.3 | TLS and reverse proxy (supports `http2 on`, `ssl_reject_handshake`) |
| certbot + `certbot.timer` | 4.0.0 | certificate; renewals are already scheduled |
| fail2ban | 1.1.0 | bans (socket is root-only, so the app runs as root) |
| supervisor | 4.3.0 | one real program: `queue-worker` (`telegram-bot.conf` is an empty file). It shows up as `queue-worker:site-worker_00`, so phase 7 must use that exact name, not the short one |
| tailscale | 1.102.4 | VPN; MagicDNS is on (`tailnet-name.ts.net`), `CorpDNS: true` |
| `ss`, `ip`, `openssl`, `curl` | | ports and owning processes, cert expiry, testing |
| Kernel PSI (`/proc/pressure/*`) | | CPU, memory and IO pressure. Very useful on this box: IO "full" averages 11.8% over 5 minutes |
| `/var/log/wtmp`, `/var/log/auth.log`, `/var/log/nginx/*.log` | | logins, SSH failures, web traffic |

### Not installed and not needed (deliberately skipped)
| Package | Why we skip it |
|---|---|
| `psutil` | everything it offers can be read from `/proc` and `/sys` directly |
| `last` / `wtmpdb` / `utmpdump` | we parse `/var/log/wtmp` with `struct` (384-byte records) |
| `sqlite3` CLI | Python's built-in `sqlite3` is enough. Optional: `apt install sqlite3` (~1.5 MB) |
| `dig` / dnsutils | `admin_dns.py --test` sends the check query itself |
| Chart libraries (uPlot/Chart.js) | none on disk. A small hand-written canvas chart (~6 KB) avoids any download |
| smartmontools, lm-sensors | VPS virtual disks and CPU don't report SMART data or temperatures |
| Docker, Node packages, Grafana/Netdata/Prometheus | far too heavy for 1 vCPU and 956 MB RAM |

**New downloads: none.** `apt install` is not needed at all.

### Resource budget
| | Budget | Notes |
|---|---|---|
| Root disk `/` (**91% full, 2.3 GB free**) | **< 100 KB** | 2 systemd units, 1 nginx site, the cert (~20 KB), an empty webroot dir |
| `/mnt/Extra20` (16 GB free) | **≈ 45 MB** | code ~300 KB, `history.db` ≈ 38 MB at full 90-day capacity (measured with 38 metrics; its size is fixed by metrics × buckets, so it cannot grow beyond that), audit log rotated at 1 MB |
| RAM | ~25–35 MB app + ~8 MB DNS | systemd caps: `MemoryMax=96M` / `32M` |
| CPU | < 2% average | `Nice=10`, idle IO class. The process table is only scanned while someone has the dashboard open |

### Current state the dashboard will show on day one
- Root disk at **91%**, so the disk warning alert fires right away. That's correct, not a bug.
- **49** packages can be upgraded, and a **reboot has been required since 2026-09-25**.
- Swap is heavily used (2.1 GB of 4.5 GB).

---

## 3. Project layout

```
/mnt/Extra20/admin/
├── PLAN.md                     this file
├── README.md                   install / operate / uninstall
├── .gitignore                  config.json, data/
├── config.example.json         template, committed
├── config.json                 real config, root:root 600 (password hash, Telegram token)
├── dashboard/
│   ├── __main__.py             CLI: serve | set-password | test-telegram | check
│   ├── config.py               load and validate config.json
│   ├── server.py               HTTP routing, static files, JSON API, security headers
│   ├── auth.py                 scrypt hashing, sessions, CSRF, login rate limit
│   ├── scheduler.py            fast (5s) / medium (60s) / slow (15m, 6h) collection loops
│   ├── collectors/
│   │   ├── system.py           host, OS, kernel, uptime, boot time, time sync
│   │   ├── cpu.py              /proc/stat, loadavg, PSI
│   │   ├── memory.py           /proc/meminfo, /proc/vmstat (swap in/out, OOM kills), PSI
│   │   ├── disks.py            /proc/mounts + statvfs, /proc/diskstats, PSI io
│   │   ├── processes.py        /proc/[pid]/{stat,status,cmdline,io}
│   │   ├── services.py         systemctl show, list-units --failed, timers, supervisorctl
│   │   ├── network.py          /proc/net/dev, /proc/net/tcp*, ss -tulpn, tailscale status --json
│   │   ├── security.py         fail2ban-client, wtmp, auth.log, apt, reboot-required
│   │   └── logs.py             journalctl -p err, per-unit logs, nginx access/error, SSL expiry
│   ├── history.py              SQLite schema, write, rollup, retention, range queries
│   ├── alerts.py               rules, states, hysteresis, cooldown, Telegram send
│   └── actions.py              allow-listed service/process actions + audit log
├── static/
│   ├── index.html  app.css  app.js  chart.js (own canvas line/area chart)
│   └── login.html
├── dnsd/
│   └── admin_dns.py            tiny authoritative responder for admin.example.com
├── deploy/
│   ├── server-dashboard.service
│   ├── admin-dns.service
│   └── nginx-admin.conf
└── data/                       history.db, audit.jsonl (created at runtime)
```

---

## 4. Step-by-step

### Phase 0: Prerequisites

**0.1 [root] Create the project folder** (`/mnt/Extra20` is root-owned):
```bash
sudo install -d -o claude -g claude -m 755 /mnt/Extra20/admin   # done 2026-10-06 as root; then chown claude:claude
```
Then copy this PLAN.md into it and run `git init`.

**0.2 [you] Get the DNS record live at your DNS host:** `A  admin → 203.0.113.10`.
On 2026-10-06 even your DNS host's own nameserver answered that the name doesn't exist (NXDOMAIN), so the record isn't published yet. To verify:
```bash
python3 -c "import socket;print(socket.gethostbyname('admin.example.com'))"
```
It must print `203.0.113.10`. Only Phase 9 (the certificate) depends on this. Everything before it can be built meanwhile.

**0.3 [you] Have the Telegram bot token and chat ID ready.** They go into `config.json` in Phase 1.4, never into code.

✅ *Done when:* the folder exists and is owned by `claude`, and git is initialised.

---

### Phase 1: Skeleton, config, CLI

1.1 Create the layout above, `.gitignore` (`config.json`, `data/`, `__pycache__/`) and the README stub.

1.2 `config.example.json`:
```json
{
  "listen": "127.0.0.1:9100",
  "public_host": "admin.example.com",
  "auth": { "username": "admin", "password_hash": "", "session_hours": 720 },
  "telegram": { "bot_token": "", "chat_id": "", "enabled": true },
  "watch": {
    "systemd": ["nginx", "app1", "app2", "redis-server", "supervisor", "smbd",
                "ssh", "tailscaled", "fail2ban", "cron", "chrony",
                "server-dashboard", "admin-dns"],
    "supervisor": "auto",
    "protected": ["ssh", "tailscaled", "nginx", "server-dashboard", "admin-dns"]
  },
  "thresholds": {
    "cpu_pct":        { "warn": 85, "crit": 95, "for_s": 300 },
    "mem_avail_pct":  { "warn": 15, "crit": 7,  "for_s": 120 },
    "swap_pct":       { "warn": 70, "crit": 90, "for_s": 300 },
    "disk_pct":       { "warn": 90, "crit": 95 },
    "inode_pct":      { "warn": 85, "crit": 95 },
    "ssl_days":       { "warn": 14, "crit": 5 },
    "load_per_core":  { "warn": 2.0, "crit": 4.0, "for_s": 300 }
  },
  "alerts": { "repeat_minutes": 60, "notify_recovery": true, "notify_logins": true }
}
```
`protected` units can be **restarted** but never **stopped** from the UI, because stopping ssh or tailscaled would lock you out.

1.3 `python3 -m dashboard` subcommands:
- `serve` starts the app (used by systemd).
- `set-password` prompts twice and writes the scrypt hash to `config.json`.
- `test-telegram` sends a test message.
- `check` runs every collector once and prints JSON. This is the main dev and debug tool.

1.4 **[root]** Create the real config:
```bash
sudo cp config.example.json config.json
sudo chmod 600 config.json
sudo chown root:root config.json
```
Then edit in the bot token and chat ID, and run `sudo python3 -m dashboard set-password`.

✅ *Done when:* `python3 -m dashboard check` runs. As non-root it will show partial data, which is expected.

---

### Phase 2: Collectors (the "really detailed" part)

Each collector returns a plain dict and never raises. On error it returns `{"error": "..."}`, so one broken source can't kill the page.

| Tab | Metric | Source | Cadence |
|---|---|---|---|
| Overview | CPU %: total and per core; user / system / iowait / **steal** / irq | `/proc/stat` deltas | 5s |
| | load 1/5/15, running/total tasks, context switches/s, interrupts/s | `/proc/loadavg`, `/proc/stat` | 5s |
| | CPU / memory / IO **pressure** (some/full, avg10/60/300) | `/proc/pressure/*` | 5s |
| | RAM: total, used, **available**, cached, buffers, dirty, slab, shmem | `/proc/meminfo` | 5s |
| | swap used %, **swap-in/out pages/s**, OOM-kill count | `/proc/meminfo`, `/proc/vmstat` | 5s |
| | disk usage + **inodes** per real mount (`/`, `/mnt/Extra20`, swap) | `/proc/mounts` + `os.statvfs` | 60s |
| | disk read/write MB/s, IOPS, **busy %**, avg wait (`sda`, `sdc`) | `/proc/diskstats` deltas | 5s |
| | host, OS, kernel, uptime, boot time, time sync status | `/etc/os-release`, `/proc/uptime`, `chronyc tracking` | 60s |
| Processes | PID, user, state, CPU %, RSS, **swap (VmSwap)**, threads, IO R/W rate, start time, full command; sortable and filterable | `/proc/[pid]/*` | 5s, **only while someone is viewing** |
| | per-service CPU and memory (cgroup totals) | `/sys/fs/cgroup/system.slice/*.service/` | 5s |
| Services | systemd: state, substate, PID, uptime, **restart count**, memory, CPU time | one `systemctl show -p … unit1 unit2 …` call | 60s (+ after an action) |
| | all **failed** units, timers (certbot, apt…) with next/last run | `systemctl list-units --failed`, `list-timers` | 60s |
| | supervisor programs: state, PID, uptime | `supervisorctl status` | 60s |
| | last 200 log lines for a chosen unit or program | `journalctl -u X -n 200`, supervisor log files | on demand |
| Network | RX/TX rate, packets, errors and drops per interface (`eth0`, `tailscale0`) | `/proc/net/dev` deltas | 5s |
| | TCP states (ESTABLISHED/TIME_WAIT/…), counts per remote IP | `/proc/net/tcp`, `/proc/net/tcp6` | 60s |
| | **listening ports → process** and whether bound public / tailnet / localhost | `ss -tulpnH` | 60s |
| | Tailscale peers: online, last seen, OS, IP | `tailscale status --json` | 60s |
| Security | fail2ban: jails, currently banned IPs, total bans | `fail2ban-client status [jail]` | 60s |
| | successful logins (user, IP, time, duration), currently logged in | parse `/var/log/wtmp`, `who` | 60s |
| | **failed SSH attempts** in the last 24h: count, top IPs, top usernames | incremental tail of `/var/log/auth.log` | 60s |
| | pending updates (total / security), reboot required + which packages | `apt list --upgradable` (niced), `/var/run/reboot-required.pkgs` | 6h + button |
| | last unattended-upgrades run | `/var/log/unattended-upgrades/` | 6h |
| Logs | journal errors and warnings in the last 24h, grouped by unit | `journalctl -p warning --since -24h -o json` | 60s |
| | nginx: requests/min, 2xx/3xx/4xx/5xx, top paths, top IPs, top user agents, 5xx list | incremental tail of `/var/log/nginx/access.log` (combined format) | 60s |
| | nginx error-log tail | `/var/log/nginx/error.log` | 60s |
| | **SSL expiry** per certificate (app1, app3, the main site, admin) | `openssl x509 -enddate` on `/etc/letsencrypt/live/*/cert.pem` | 6h |

Implementation notes:
- **Incremental log tails:** remember the inode and offset, and handle logrotate when the inode changes. Logs are never re-read from the start.
- **External commands** run with a timeout (5s) and `nice`, and always with argument lists, never a shell.
- **Optional later:** the default nginx `combined` format has no `$host` or `$request_time`. Adding a custom `log_format` would give per-site stats and response times, but it changes the global nginx config, so it is left out of v1.

✅ *Done when:* `sudo python3 -m dashboard check` prints every section with real values and no `error` keys.

---

### Phase 3: History (SQLite, tiered, 90 days)

3.1 `data/history.db` in WAL mode, with `auto_vacuum=INCREMENTAL`. The layout is **narrow**: one row per metric per timestamp (`s5(m, ts, v)`, `m1` / `m15` `(m, ts, avg, max)`, primary key `(m, ts)`, `WITHOUT ROWID`). This lets per-disk and per-interface metrics appear without schema changes. There are 38 metrics today: cpu (busy/user/system/iowait/steal), load 1/5, memory and swap (used, available, cached, swap-in/out, major faults), pressure (cpu/memory/io), IO per device (read, write, busy %, await), traffic per interface and used % per mount. Inode usage is not charted because it changes too slowly.

| Table | Resolution | Kept for | Rows (max, 38 metrics) |
|---|---|---|---|
| `s5`  | 5 s  | 24 h | 691,200 |
| `m1`  | 1 min (avg + max) | 7 d | 403,200 |
| `m15` | 15 min (avg + max) | 90 d | 345,600 |

3.2 The fast loop buffers samples in memory and **commits every 60s**, to keep disk writes low. Rollups (`m1` from `s5`, `m15` from `m1`) and retention deletes run every minute. `PRAGMA incremental_vacuum` runs daily.

3.3 API: `GET /api/history?metrics=cpu,mem_avail&range=1h|6h|24h|7d|30d|90d`. It picks the tier automatically (the coarsest one that is still fine enough, so the fewest rows are read) and returns at most ~600 points per series: 1h and 6h use the 5 s tier, 24h uses the 1-minute tier, and 7d and longer use the 15-minute tier. Max values are stored too, so short spikes don't vanish in the 15-minute averages.

3.4 Service up/down events and alerts are stored in an `events` table, which the charts show as markers.

✅ *Done when:* the DB grows to the expected size after a few hours (check `ls -la data/`) and the range queries return quickly (< 50 ms).

---

### Phase 4: HTTP server and authentication

4.1 `ThreadingHTTPServer` on `127.0.0.1:9100`. Routes:
- `GET /login`, `POST /login`, `POST /logout`
- `GET /`: the app (requires a session)
- `GET /api/live?tab=overview`: everything for one tab in one call
- `GET /api/history`, `GET /api/logs?unit=…`
- `POST /api/action/*`

4.2 Auth:
- The password is hashed with `hashlib.scrypt` (n=2^14, r=8, p=1, 16-byte salt) and checked with `hmac.compare_digest`. A wrong username does the same amount of work as a wrong password, so timing does not reveal which was wrong.
- The session is a random token (`secrets.token_urlsafe(32)`). Only its SHA-256 is stored, in `data/auth.db` (mode 600), so a restart does not log you out. At most 20 sessions exist at once, and changing the username or password ends all of them.
- The cookie is `__Host-sid` with `Secure; HttpOnly; SameSite=Lax; Path=/`, lasting 30 days (configurable). It is `Lax` rather than `Strict` so that a link tapped in a Telegram alert opens the dashboard already signed in. Cross-site POSTs are blocked by the CSRF and Origin checks below, not by the cookie alone.
- **CSRF:** every POST must carry an `X-CSRF-Token` header that matches the session's token, an `Origin` that is our own, and no cross-site `Sec-Fetch-Site`. The `Host` header must also be ours, which blocks DNS-rebinding.
- **Login throttling:** 5 failures per 15 minutes per client address, and 30 per 15 minutes overall. The overall limit exists because any local process can forge the `X-Real-IP` header, so rotating fake addresses would otherwise defeat the per-address limit. Failures are logged with the address only, never the typed text (people paste passwords into the username box). Failed and successful logins are saved as events, ready for the Telegram "new device" alert in phase 6.
- The client address is taken from `X-Real-IP` only when the connection comes from 127.0.0.1 (nginx).
- At most 24 connections are served at once; extra ones are dropped, because each is a thread on a 1 GB machine. Slow clients are cut off after 15 s.

4.3 Headers:
- `Content-Security-Policy: default-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`
- `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, and cross-origin isolation headers
- `Cache-Control: no-store` on everything except static files (`no-cache`)

✅ *Done when:* the routes behave as expected under `curl`. Without a cookie, requests go to `/login` (302) or get 401. A wrong password 6 times leads to a lockout. A POST without the CSRF token gets 403.

---

### Phase 5: Frontend (tabs, mobile first)  ✅ built and checked in a real browser

Plain HTML, CSS and JavaScript modules in `static/` (no framework, no build step, no third-party code). Dark theme, phone-first layout, six tabs routed by the URL hash (`#overview` ... `#logs`) so reload and the back button work.

- **Refresh:** only the visible tab is fetched, every 5 s, and polling pauses while the browser tab is hidden (this saves phone battery and server load; it also means a tab opened in the background shows "connecting…" until you look at it). The header shows `live` / `updated Ns ago` / `no connection`.
- **Alert banner:** `/api/live` carries the current alerts (`dashboard/alerts.py`, `evaluate()`), shown above every tab: red for critical, amber for warning, blue for notices, collapsed unless something is critical. Phase 6 adds timing, de-duplication and Telegram around the same rules.
- **Overview:** host, CPU, memory, swap, one card per filesystem, disk activity, network and pressure cards, coloured by the configured thresholds; nine history charts (CPU, load, memory and swap, swap traffic, disk busy, disk throughput, network, pressure, disk space) with a 1h/6h/24h/7d/30d/90d range picker.
- **Processes:** every process with sortable columns and a filter box, plus RAM, swap and CPU per service.
- **Services:** systemd units (status, uptime, CPU, RAM, swap, restarts), failed units, supervisor programs, timers, and a log viewer per service. Start, stop and restart buttons arrive with phase 7.
- **Network:** interfaces with a traffic chart, every listening port with who can reach it, connection states and top remote addresses, Tailscale devices.
- **Security:** pending updates and reboot flag, SSH login statistics, fail2ban, who is signed in, login history, failed attempts by address and username.
- **Logs & web:** certificate expiry, nginx traffic (hourly and per-minute charts, top paths, clients, browsers, recent 5xx, error log), the system journal grouped by source, and firewall blocks counted separately so they cannot drown real messages.

Browser extensions matter: an ad blocker hides anything whose class or id contains words like "banner" or "ad", which silently hid the alert bar in the owner's Chrome. The alert bar is therefore called `statusbar`; avoid ad-like words in names. Flag attributes (`disabled`, `hidden`, ...) are set from truthiness only, because an empty string would still switch them on.

Safety rules the code follows (and `tests/test_frontend.py` enforces): text is only ever inserted as text nodes, never as HTML (much of the data is attacker-controlled); no inline styles or scripts (the CSP forbids them); every collector failure shows as "Unavailable" in its own card only.

Bugs found by looking at the real page and fixed: read-only disks were reported because systemd's sandbox makes every filesystem look read-only from inside (now read from the host's mount table), a stray "null" printed by `replaceChildren`, chart axis labels clipped and rounded in bytes instead of KB/MB steps, and very tall command lines in the process table.

✅ *Done when:* every tab renders with live data and no horizontal page scroll at 390 px wide (verified in Chrome; the small-screen rules were checked by constraining the page width).

---

### Phase 6: Alerts and Telegram  ✅ built and tested end to end

Two layers: `dashboard/alerts.py` decides what is wrong *right now* (`evaluate()`, a pure function), and `dashboard/alertmanager.py` remembers (how long, who was told, when to repeat, what is muted) and delivers messages. State lives in `data/alert_state.json` (mode 600), so restarting the dashboard does not resend anything.

**When a message is sent**
- A condition must last its `for_s` before it counts (CPU 5 min, low RAM 2 min, swap 5 min, load 5 min; services 30 s; disk, inodes and certificates at once). A spike shorter than that never alerts, and no "recovered" follows it.
- **Hysteresis:** once active, an alert only clears when the value is clearly back in the safe zone (3 points for CPU, memory and swap; 1 point for disk, inodes and certificate days; 0.25 for load), and then it must stay clear for 30 s. A reading hovering at a threshold does not flap.
- **Escalation** (warning → critical) is announced; de-escalation is silent. **Repeats:** critical every `repeat_minutes` (60), warnings every 4× that, notices (like "reboot required") once a day. **Recovery** is sent only if a person was told about the problem, and only for warnings and criticals.
- A burst of new alerts (after a reboot, say) is sent as one digest message, never twenty.
- A collector that fails or returns nothing **holds** its alerts instead of clearing them, so "no data" is never reported as "recovered".
- If Telegram is not configured, nothing is marked as sent: the first alerts go out as soon as it is.

**Rules**: CPU, load per core, RAM available, swap, out-of-memory kills, disk and inode use per filesystem, certificate expiry, systemd units that are enabled but not running (warning if stopped cleanly, critical if failed or crashed; disabled units stay quiet), other failed units, supervisor programs, Tailscale, security updates, reboot required (notice).
**One-off messages**: a successful SSH login from an address never seen before (history is learned silently on the first run, and logins made while the dashboard was down are included next time), a new device signing in to the dashboard, a dashboard sign-in lockout (once an hour per address), and a fail2ban ban spike (5 or more in 10 minutes). Failed SSH attempts do not alert: password login is off, so they are information.

**Muting** (banner buttons): 1 hour, 24 hours or until fixed. A muted alert stays visible, is dimmed, and sends nothing, not even a recovery message. A mute "until fixed" ends when the alert clears, and expires after 10 minutes if the alert never appeared, so a forgotten mute cannot hide a later real failure. Phase 7 will use it so a service stopped from the dashboard does not page you.

**Delivery**: Telegram messages go through a background queue with retries (now, 5 s, 30 s, 2 min), so a slow Telegram can never delay data collection. Text is HTML-escaped. The Security tab has an Alerts and Telegram panel with a "Send test alert" button, the delivery status and the last 7 days of alert events; alert opens and resolutions also appear as markers on the charts.

✅ *Done when:* the test message arrives. Stopping a service from a shell (`sudo systemctl stop smbd`) shows a warning on the dashboard within about 90 s (a minute for the next check, plus 30 s so a quick restart does not count) and a Telegram message; starting it again sends the recovery. A crash shows as critical instead.

---

### Phase 7: Actions (services and processes)  ✅ built and tested

All the rules live in `dashboard/actions.py`; the web layer only checks the session, the CSRF token and the origin (the same as every state-changing request) and hands over.

**Services** (`POST /api/action/service {kind, name, op}`)
- Only units listed in `watch.systemd` that are installed, and supervisor programs that currently exist. Names must match `[A-Za-z0-9][A-Za-z0-9_.:@-]*` exactly (so never an option or a shell fragment), are passed as an argument list after `--`, and never through a shell.
- **Protected services** (`watch.protected`, plus the dashboard and its DNS responder) can be restarted but **never stopped**. Restarting one shows a warning that says what you might lose.
- Stopping a service from the dashboard mutes that unit's alert until it is running again (Phase 6). The dashboard restarting itself uses `--no-block` and the page reloads once it answers again.

**Processes** (`POST /api/action/process {pid, signal: TERM|KILL, start_ticks, name}`)
- A process is identified by PID **and** its exact start tick (a number the kernel never repeats for one PID) and its name. The process is opened first (`pidfd_open`), checked, and the signal is sent *through that handle*, so a PID recycled between "I looked at the table" and "I clicked" can never be hit. The plan used a rounded start time; this is stricter.
- Refused: PID 1, kernel threads, the dashboard and every ancestor of it, core daemons (`systemd*`, `sshd`, `tailscaled`, `init`, `dbus-daemon`), and **any process that belongs to a protected service** (found through its cgroup, so workers and children count too).
- Only SIGTERM and SIGKILL exist. The UI offers Terminate first and shows Force kill only if the process is still running afterwards. The server reports whether the process really exited (it waits up to 2 s on the handle).

**Everywhere**: one action at a time (a second one gets "busy"), at most 20 a minute, every attempt (allowed, refused or failed) is written to `data/audit.jsonl` (mode 600, rotated at 1 MB, three old files kept; a write failure is logged but never blocks you, because a full disk is exactly when you need to kill something). Telegram is told about every action that ran, and about refused or failed ones, but not about typos or stale tables. The Services tab shows the last 50 entries.

**Interface**: every action opens a confirmation naming the exact target, with Cancel focused. Results appear as a toast. Click a process row for its details, belongs-to service and the buttons.

✅ *Done when:* restarting `smbd` from the UI works and appears in the audit log and on Telegram. A stop on `ssh` is refused, and a kill on PID 1 is refused.

---

### Phase 8: DNS responder for Split DNS

`dnsd/admin_dns.py` (about 300 lines, standard library only) is a tiny authoritative DNS server for exactly one name:
- Listens on UDP and TCP port 53 of the server's Tailscale address (`100.64.0.1`). It binds with `IP_FREEBIND`, so it starts at boot even before Tailscale has brought its interface up.
- `admin.example.com A` → `100.64.0.1`, TTL 300. **No AAAA record is served** (the answer is "no data"), so every client uses IPv4 and the nginx allow-list only needs each device's IPv4 address. If it also answered with IPv6 and one device's IPv6 address were missing from the allow-list, that device's browser would connect, get cut off and not fall back.
- Other record types for that name (HTTPS, TXT, ...) get an empty answer with an SOA record, so resolvers cache "nothing here" for 60 s instead of asking again and again.
- Every other name gets REFUSED. No recursion, no forwarding, no caching. Replies are never larger than a few dozen bytes more than the query, and it never answers a packet that is itself a response.
- Runs as a throwaway unprivileged user (`DynamicUser`) whose only privilege is binding port 53, with a 48 MB memory cap.

Tested: 13 unit tests (including 20,000 mutated packets that must never crash it or grow the reply) and interoperability with `dig`, `host`, `nslookup` and Node's c-ares resolver, over UDP and TCP, including case-randomised names.

Install: `sudo deploy/install-admin.sh dns`. It installs the unit, starts it and queries it. 

✅ *Done when:* the script's last lines say `UDP ... OK`, `TCP ... OK` and `other names ... OK (refused)`.

---

### Phase 9: nginx and HTTPS certificate

Requires step 0.2: the public DNS record must resolve (it does since 2026-10-06).

The files are in `deploy/` and were tested against a private nginx instance (allow-list, proxying, forged-header, rate-limit, Host and body-size checks). `deploy/install-admin.sh` installs them in stages. Each stage runs `nginx -t` on the **whole** configuration before reloading, and restores the previous state if the test fails, so a mistake cannot take the other sites down.

| Stage | Command (as root) | What it does |
|---|---|---|
| 9.1 | `sudo deploy/install-admin.sh http` | Creates the challenge folder `/var/www/letsencrypt` and installs a port-80-only site (`nginx-admin-http.conf`) |
| 9.2 | `sudo deploy/install-admin.sh cert` | Dry run, then the real Let's Encrypt request in webroot mode (so certbot never edits nginx files); the existing `certbot.timer` renews it |
| 9.3 | `sudo deploy/install-admin.sh https` | Installs the proxy snippet to `/etc/nginx/snippets/` and the real site (`nginx-admin.conf`) |

Design notes:
- **The allow-list is the access control.** A `geo` block lets in exactly two addresses per device (laptop 100.64.0.2 and its IPv6, phone 100.64.0.3); everyone else gets the connection closed (`return 444`). Add the phone's `fd7a:` address if it ever connects over IPv6.
- **nginx does not bind to the Tailscale IP.** It starts before `tailscaled`, and a failed bind would stop every site.
- **No cross-disk dependency:** the proxy snippet lives in `/etc/nginx/snippets/` on the root disk, so nginx still starts if `/mnt/Extra20` is not mounted yet.
- **Login throttling in nginx:** 10 POSTs per minute per address with a burst of 5, keyed on the request method so that loading pages never counts. The app has its own stricter limit on top.
- **Until phases 8 and 11 are done,** `admin.example.com` still resolves to the server's public address, so your devices connect over the normal internet and are not recognised: you will see a closed connection, not the login page. That is expected.
- No `http2` directive: it adds nothing for this page and avoids any interaction with the other sites that share port 443.
- `admin.access.log` keeps the dashboard's own polling out of the main nginx log, so it doesn't distort the traffic statistics.

✅ *Done when:* `nginx -t` passes, the existing sites still answer (`curl -sI https://app3.example.com`), and from a device that is not on the allow-list the connection is closed.

---

### Phase 10: systemd service for the app

10.1 `deploy/server-dashboard.service` (installed by `sudo deploy/install-admin.sh service`, which first checks that a password is set):
- `User=root`, because killing any process, restarting units and reading the fail2ban and supervisor sockets all need it. The exposure is limited by the loopback-only binding, the nginx allow-list, the login, the CSRF and Origin checks, the allow-lists for actions and the audit log.
- `RequiresMountsFor=/mnt/Extra20`, so it never starts before its disk.
- `MemoryMax=192M` with `OOMPolicy=continue`. The app itself needs about 20 MB, but `apt list --upgradable` briefly needs about 80 MB in the same cgroup, so the 96 MB in the first draft was too tight.
- `NoNewPrivileges`, `PrivateTmp`, `ProtectHome` and `ProtectSystem=strict` with only `data/` writable. Hardening is deliberately modest because the app has to reach many system sockets.
- `Nice=10` and idle IO priority, so the dashboard never competes with the sites it monitors.

10.2 Install and start: `sudo deploy/install-admin.sh service`. Check with `systemctl status server-dashboard` and `curl -s http://127.0.0.1:9100/healthz`.

✅ *Done when:* the service is active, it uses < 40 MB RSS, and it starts again on its own after `sudo systemctl kill server-dashboard`.

---

### Phase 11: Tailscale Split DNS (Tailscale admin console)

11.1 Go to https://login.tailscale.com/admin/dns → **Nameservers → Add nameserver → Custom…**
- Nameserver: `100.64.0.1`
- Turn on **Restrict to domain** (Split DNS) and enter `admin.example.com`
- Save

11.2 On your phone and laptop, make sure the Tailscale app has **Use Tailscale DNS settings** turned on (it is by default).

11.3 Check from the laptop, before opening the browser:
```bash
nslookup admin.example.com 100.64.0.1     # asks the responder directly: must answer 100.64.0.1
nslookup admin.example.com                    # asks the system resolver: must also answer 100.64.0.1
```
- If the first one times out, the server's firewall (UFW) is probably dropping port 53 from the tailnet. On the server, as root: `ufw allow in on tailscale0 to any port 53`.
- If the first works and the second shows `203.0.113.10`, Split DNS is not active on that device: check the "Use Tailscale DNS settings" switch, then toggle Tailscale off and on.

11.4 Browser gotchas:
- **Chrome with a custom "Secure DNS" provider** (Settings → Privacy and security → Security → Use secure DNS) bypasses Tailscale's DNS. Leave it on "Automatic" or turn it off.
- **A stale DNS cache is the usual first-time problem.** The public record has a 30-minute TTL, so anything that looked the name up before Split DNS was active (Chrome shows `ERR_HTTP2_PROTOCOL_ERROR`, Safari "can't open the page") keeps the public address for up to 30 minutes and connects over the internet, where nginx hangs up on it. `nslookup` bypasses the cache and looks fine, so do not trust it for this. On the Mac: `sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder`, then check what apps get with `dscacheutil -q host -a name admin.example.com`, then Chrome's `chrome://net-internals/#dns` → Clear host cache. On the phone: Airplane Mode on and off. To confirm from the server, `tail /var/log/nginx/admin.access.log`: a first column that is your home address (not a `100.x` address) with status 444 means the device did not use the tailnet.

✅ *Done when:* with Tailscale on, your phone's browser opens https://admin.example.com to the login page, with a valid certificate and no warning.

---

### Phase 12: Final verification checklist

- [x] **Reboot check (done 2026-10-06).** The pre-flight found that the data disk `/mnt/Extra20` (a VPS provider volume) had been mounted by hand and was not in `/etc/fstab`, so a reboot would have brought the server up without the dashboard or the sites on it. Fixed with `UUID=... /mnt/Extra20 ext4 defaults,nofail 0 2`. Lesson: before any reboot, compare what is running with what is configured to start (`systemctl is-enabled`, `/etc/fstab`, supervisor configs).
- [ ] **Ownership of the code:** the service runs as root, but the code folder belongs to the `claude` user, so anyone who can write there could get root at the next restart. When the build is finished, run `sudo chown -R root:root /mnt/Extra20/admin` (the service is the only writer of `data/`) and keep a separate working copy for further development.
- [ ] Phone with Tailscale on → login → every tab shows live data.
- [ ] Phone with **Tailscale off** (mobile data) → admin.example.com doesn't load (connection closed).
- [ ] From any non-tailnet machine, `curl -sk https://admin.example.com` gets an empty reply (444).
- [ ] `http://admin.example.com/` from the internet → 444, except `/.well-known/acme-challenge/`.
- [ ] `sudo certbot renew --dry-run` succeeds.
- [ ] Wrong password ×6 → locked out. Telegram reports a login from a new IP.
- [ ] Restart `smbd` from the UI → works, is audit-logged and is reported on Telegram.
- [ ] Stopping `ssh` is refused, and killing PID 1 is refused.
- [ ] Telegram test alert arrives. A stopped service raises CRIT, and starting it sends a recovery message.
- [ ] Charts show 1h, 24h and, after a while, 7d ranges. `history.db` stays under 40 MB.
- [ ] `sudo reboot` → nginx, admin-dns, server-dashboard and all existing sites come back on their own.
- [ ] Dashboard footprint: < 40 MB RAM and < 2% CPU on average (check it on its own Processes tab).

---

## 5. Operating and maintenance (goes into README)

- **Change the password:** `sudo python3 -m dashboard set-password`, then `sudo systemctl restart server-dashboard`.
- **Update:** `git pull`, then `sudo systemctl restart server-dashboard`.
- **App logs:** `journalctl -u server-dashboard -f`.
- **Back up:** only `config.json` matters. The history is disposable.
- **Uninstall:**
  ```bash
  sudo systemctl disable --now server-dashboard admin-dns
  sudo rm /etc/systemd/system/{server-dashboard,admin-dns}.service
  sudo rm /etc/nginx/sites-enabled/admin /etc/nginx/sites-available/admin
  sudo systemctl reload nginx
  sudo certbot delete --cert-name admin.example.com
  ```
  Then remove the Split DNS entry in Tailscale, the your DNS host record and the project folder.

## 6. Build order and who does what

| Phase | Who | Blocked by |
|---|---|---|
| 0.1 folder, 0.2 DNS record, 0.3 Telegram details | **you** | — |
| 1–7 app code | Claude (needs `sudo` only for 1.4 and full-data tests) | 0.1 |
| 8 DNS responder | Claude writes it, **you** install it [root] | 1 |
| 9 nginx + cert | Claude writes the configs, **you** run them [root] | 0.2 DNS live |
| 10 systemd | Claude writes it, **you** install it [root] | 1–7 |
| 11 Tailscale Split DNS | **you** (admin console) | 8 |
| 12 verification | together | all |
