# Server admin dashboard

Live monitoring and control for a Linux server, in a browser. It is built to sit behind nginx and be reachable only over [Tailscale](https://tailscale.com).
Python standard library only: there is nothing to install with pip.

## Screenshots

IP addresses are blurred.

| Users | Services |
|---|---|
| ![Users tab](docs/images/users.png) | ![Services tab](docs/images/services.png) |

![Agents tab](docs/images/agents.png)

## What it does

| Tab | Shows |
|---|---|
| Overview | CPU, memory, swap, disks, disk and network activity and pressure, with live graphs; cards can be dragged into any order |
| Processes | All processes and resource use by service; terminate or force-kill a process |
| Services | systemd services and timers, supervisor programs, failed units, a log of actions taken from the dashboard, and the Power card |
| Network | Interfaces and throughput, connections, listening ports, Tailscale |
| Security | Alerts, signed-in users, failed and successful SSH logins, fail2ban bans, updates and reboot-needed, login history |
| Logs & web | nginx traffic, errors and top clients and paths, firewall blocks, SSL certificate expiry, system journal warnings |
| Agents | Tokens and hours per day for each Claude Code sub-agent type (see below) |
| Users | Who is signed in, every account, and account management |

Alerts (CPU, memory, swap, disk, inodes, certificate expiry, load, services going down, logins) are sent to Telegram, with a recovery message when
the problem clears. Everything done from the dashboard is written to an audit log (`data/audit.jsonl`).

## Requirements

- Linux with systemd, Python 3.10 or newer, nginx, and Tailscale on the server and on the devices that open the dashboard
- Root: the service runs as root so that it can end processes, restart services and manage accounts

## Install

```bash
git clone https://github.com/ProgrammerFoodie/serveradmindash.git
cd serveradmindash

# 1. Real config: root-only, because it holds the password hash and the Telegram token
sudo cp config.example.json config.json
sudo chown root:root config.json
sudo chmod 600 config.json
sudo nano config.json                     # public_host, telegram.bot_token, telegram.chat_id

# 2. Login password (scrypt-hashed into config.json)
sudo python3 -m dashboard set-password

# 3. Check
sudo python3 -m dashboard test-telegram   # sends a test message
sudo python3 -m dashboard check           # runs every collector once, prints JSON
```

Then put it on the web. Copy `deploy/local.env.example` to `deploy/local.env` (your domain and the server's Tailscale address) and
`deploy/allowed-devices.example.txt` to `deploy/allowed-devices.txt` (the Tailscale addresses that may open it). Both files are git-ignored. Run the
stages one at a time, as root, and check each result:

```bash
deploy/install-admin.sh render /tmp/out   # write the filled-in files for review; installs nothing
deploy/install-admin.sh http              # port 80 site, so the certificate can be issued
deploy/install-admin.sh cert              # Let's Encrypt certificate
deploy/install-admin.sh https             # the real site (443, only the listed Tailscale devices)
deploy/install-admin.sh service           # install and start the dashboard
deploy/install-admin.sh dns               # optional: split-DNS responder for Tailscale
```

`deploy/server-dashboard.service` expects the program in `/mnt/Extra20/admin`. If you keep it somewhere else, change `WorkingDirectory`,
`RequiresMountsFor` and `ReadWritePaths` in that file (and in `deploy/admin-dns.service.template`) before running the `service` and `dns` stages.

## Operating it

| Task | How |
|---|---|
| Change the login password | `sudo python3 -m dashboard set-password`, then `sudo systemctl restart server-dashboard` |
| Change the inactivity sign-out (default 15 min) | set `auth.idle_minutes` (1 to 1440) in `config.json`, then restart the service |
| Change Telegram or alert settings | edit `config.json` as root, then restart the service (the config is read at start) |
| Update the code | `git pull`, then `sudo systemctl restart server-dashboard` |
| Dashboard logs | `journalctl -u server-dashboard -f` |
| What was done from the UI | the Services tab, or `data/audit.jsonl` |
| Back up | only `config.json` matters; history, alert state and sessions in `data/` can be rebuilt |
| Add a device that may open it | add its Tailscale address to the `geo $admin_allowed` block in `/etc/nginx/sites-available/admin`, then `nginx -t && systemctl reload nginx` |

Never commit `config.json` or `data/`; both are in `.gitignore`.

## Commands

| Command | What it does |
|---|---|
| `python3 -m dashboard check [names…]` | Run collectors once and print JSON. Exit code 1 if any section has an error. |
| `python3 -m dashboard set-password` | Set the login username and password. |
| `python3 -m dashboard test-telegram` | Send a test Telegram message. |
| `python3 -m dashboard serve` | Run the web app (used by systemd). Needs a password set first. |

Tests (standard library only), run from the project folder: `python3 -m unittest`. Without `sudo`, `check` falls back to `config.example.json` and shows partial data.

## Admin tools

Reboot, account management, SSH keys and a config editor are each off unless `config.json` turns them on; the web page can never switch one on:

```json
"admin": { "power": true, "users": true, "ssh_keys": true, "private_keys": true, "configs": true }
```

A missing block, or a missing key, means off. Edit it as root and restart the service.

**Power** (`admin.power`): on the Services tab, a Power card with *Reboot in 1 minute* (a bar with a countdown and a Cancel button
shows on every page), *Reboot now*, and *Restart all services*. Each asks you to type the server's name. Restart-all goes through the
watched services one by one (data stores first, nginx last), carries on after a failure, and never touches the dashboard, its DNS
responder or tailscaled. To choose the order yourself: `"admin": { "power": true, "restart_order": ["redis-server", "app1", "nginx"] }`.
Telegram hears about a reboot before it happens and when the server is back.

**Users** (`admin.users`): the Users tab shows who is signed in (SSH, console and this dashboard, with a "you" marker) and every account:
sudo rights (and whether sudo needs a password), locked, expired or empty passwords, last login, SSH key count, home folder and running
processes. Click an account for everything about it. Password hashes are never sent to the page. Without root the dashboard still shows
what it can and says what it could not read.

The same tab manages accounts (all behind a confirmation, the dangerous ones behind typing the user name): add a user, change a password
(root's too), lock and unlock, ban (lock and sign out now), rename, change the home folder, remove, and end a session or another browser's
dashboard sign-in. Only login users can be locked, banned, renamed, moved or removed; the last user with full sudo rights who can log in is
always protected; renaming or removing is refused while something still runs as the user or mentions the name, and the refusal says where.
Home folders are chosen in a folder browser (Browse… next to the path): your disks with free space, a clickable path, and greyed-out folders that say why
they cannot be used. Before it changes anything the dashboard checks that it can write there, and if a command fails half way it looks at what is true
now and puts the account back, instead of leaving it pointing at a folder that is not there.

### Homes on another disk

The dashboard runs inside a systemd sandbox that makes the whole file system read-only except the folders listed in `ReadWritePaths` in
`deploy/server-dashboard.service` (`/etc`, `/home`, `/root` and the disk the program lives on). A home folder on any other disk can only be created,
moved or deleted from the dashboard if that disk's mount point is added to that list (`sudo systemctl edit server-dashboard`, add
`[Service]` and `ReadWritePaths=/mnt/other`) and the service restarted. The dashboard checks before it acts and says so if a folder is read-only to it.

## Agents tab

A read-only tab with tokens and hours per day for each Claude Code sub-agent type, over a window you pick (1 day, 7 days, 30 days, 365 days or year to
date), as stacked bars with a Tokens/Hours toggle, plus a per-agent table and the latest runs. `dashboard/collectors/agents.py` reads the sub-agent
transcripts under `/home/*/.claude/projects` and `/root/.claude/projects` every minute and keeps one row per run in `data/agents.db` (365 days of history),
so history survives Claude Code deleting old transcripts. Time is wall-clock first-to-last message, split at midnight; tokens are input +
output + cache read + cache write. Only the agent type and a short task title leave the collector, never transcript text.

## Locking it down

The service runs as root, so whoever can write to the program folder can run code as root at the next restart. Once you are done editing, make the folder root-owned:

```bash
sudo chown -R root:root /path/to/serveradmindash
```

From then on, edit as root (or keep a separate working copy) and restart the service to apply changes.

## License

See [LICENSE](LICENSE).
