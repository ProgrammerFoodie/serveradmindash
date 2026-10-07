"""Command line: python3 -m dashboard {serve,set-password,test-telegram,check}."""

import argparse
import getpass
import json
import os
import socket
import sys
import time

from . import __version__, auth, collectors, config
from .alerts import TelegramError, send_telegram


def _warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


def _summarize(name: str, data: dict) -> str:
    """One line per section: ok/ERROR plus a few headline numbers."""
    if "error" in data:
        return f"ERROR  {data['error']}"
    picks = {
        "cpu": lambda d: f"busy {d['total']['busy'] if d['total'] else '?'}%, load {d['load']}",
        "memory": lambda d: f"used {d['used_pct']}%, swap {d['swap_used_pct']}%, swap-in {d['swap_in_Bps']} B/s",
        "disk_io": lambda d: ", ".join(f"{x['device']}({x['role'] or '-'}) busy {x['busy_pct']}%" for x in d["devices"]),
        "net_io": lambda d: ", ".join(f"{x['name']} rx {x['rx_Bps']} B/s" for x in d["interfaces"]),
        "processes": lambda d: f"{d['count']} processes, top: {d['processes'][0]['name'] if d['processes'] else '-'}",
        "cgroups": lambda d: f"{len(d['services'])} services, biggest: {d['services'][0]['name']}",
        "system": lambda d: f"{d['os']}, up {d['uptime_s'] // 86400}d",
        "disks": lambda d: ", ".join(f"{m['mount']} {m['used_pct']}%" for m in d["mounts"]),
        "systemd": lambda d: f"{sum(u['active'] == 'active' for u in d['watched'])}/{len(d['watched'])} active, "
                             f"failed: {[f['unit'] for f in d['failed']]}",
        "supervisor": lambda d: ", ".join(f"{p['name']} {p['state']}" for p in d["programs"]),
        "sockets": lambda d: f"{len(d['listening'])} listening, {d['tcp_states'].get('ESTABLISHED', 0)} established",
        "tailscale": lambda d: f"{d['state']}, {sum(p['online'] for p in d['peers'])}/{len(d['peers'])} peers online",
        "fail2ban": lambda d: f"{len(d['jails'])} jails, {d['banned_total']} banned",
        "logins": lambda d: f"{len(d.get('history', []))} history rows, {len(d.get('sessions', []))} sessions"
                            + (f" (history: {d['history_error']})" if "history_error" in d else ""),
        "ssh_auth": lambda d: f"24h: {d['accepted']} accepted, {d['failed']} failed, {d['invalid_user']} invalid user",
        "journal": lambda d: f"24h: {d['errors']} errors, {d['warnings']} warnings in {len(d['units'])} units",
        "nginx": lambda d: f"24h: {d['requests']} requests, status {d['status']}",
        "updates": lambda d: f"{d['count']} upgradable ({d['security_count']} security), reboot required: {d['reboot_required']}",
        "ssl": lambda d: ", ".join(f"{c['name']} {c.get('days_left')}d" for c in d["certificates"]),
    }
    try:
        return "ok     " + picks[name](data) if name in picks else "ok"
    except (KeyError, IndexError, TypeError) as e:
        return f"ok     (summary unavailable: {e!r})"


def cmd_check(args) -> int:
    cfg, path = config.load(allow_example=True)
    if path == config.EXAMPLE_PATH:
        _warn(f"using {path.name}; config.json is missing or root-only")
    if os.geteuid() != 0:
        _warn("not running as root; some sections will be partial")
    available = collectors.create()
    names = args.only or list(available)
    unknown = set(names) - set(available)
    if unknown:
        print(f"unknown collector(s): {', '.join(sorted(unknown))}; "
              f"available: {', '.join(available)}", file=sys.stderr)
        return 2

    # Rate-based (fast) collectors need two samples; take both before the slower
    # collectors run, so their work doesn't distort the measured interval.
    fast = [n for n in names if available[n].cadence == "fast"]
    for name in fast:
        collectors.run(available[name], cfg)
    if fast:
        time.sleep(1)
    result = {name: collectors.run(available[name], cfg) for name in fast}
    result.update({name: collectors.run(available[name], cfg) for name in names if name not in result})

    if args.summary:
        for name, data in result.items():
            print(f"{name:<11} {_summarize(name, data)}")
    else:
        print(json.dumps(result, indent=2))
    return 1 if any("error" in section for section in result.values()) else 0


def cmd_set_password(args) -> int:
    cfg, _ = config.load()
    username = input(f"Username [{cfg['auth']['username']}]: ").strip() or cfg["auth"]["username"]
    password = getpass.getpass("New password: ")
    if len(password) < auth.MIN_PASSWORD_LEN:
        print(f"Password must be at least {auth.MIN_PASSWORD_LEN} characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Repeat password: ") != password:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    cfg["auth"]["username"] = username
    cfg["auth"]["password_hash"] = auth.hash_password(password)
    config.save(cfg)
    print(f"Password for '{username}' saved. If the dashboard service is already running, "
          "apply it with: sudo systemctl restart server-dashboard")
    return 0


def cmd_test_telegram(args) -> int:
    cfg, _ = config.load()
    try:
        send_telegram(cfg, f"✅ <b>{socket.gethostname()}</b>: test alert from the admin dashboard")
    except TelegramError as e:
        print(f"Telegram test failed: {e}", file=sys.stderr)
        return 1
    print("Telegram test message sent.")
    return 0


def cmd_serve(args) -> int:
    import logging
    import signal
    import threading

    from .actions import Actions
    from .alertmanager import AlertManager, Notifier
    from .audit import Audit
    from .history import History
    from .scheduler import Scheduler
    from .server import App, make_server

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("dashboard")
    cfg, _ = config.load()
    if not cfg["auth"]["password_hash"]:
        print("No password is set. Run: python3 -m dashboard set-password", file=sys.stderr)
        return 1
    if os.geteuid() != 0:
        log.warning("not running as root: some sections will be partial and actions will fail")

    config.DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(config.DATA_DIR, 0o700)    # also fixes a folder that was created with looser permissions
    history = History(config.DATA_DIR / "history.db")
    sessions = auth.Sessions(config.DATA_DIR / "auth.db",
                             auth.fingerprint(cfg["auth"]["password_hash"], cfg["auth"]["username"]),
                             cfg["auth"]["session_hours"],
                             cfg["auth"].get("idle_minutes", auth.DEFAULT_IDLE_MINUTES))
    notifier = Notifier(cfg)
    alerts = AlertManager(cfg, notifier, history, state_path=config.DATA_DIR / "alert_state.json")
    scheduler = Scheduler(cfg, history, alerts=alerts)
    actions = Actions(cfg, scheduler, alerts, Audit(config.DATA_DIR / "audit.jsonl"))
    server = make_server(App(cfg, scheduler, history, sessions, alerts=alerts, actions=actions))

    def shut_down(signum, _frame):
        log.info("signal %s: shutting down", signal.Signals(signum).name)
        # shutdown() waits for serve_forever() to exit, so it cannot run on the thread that is inside it.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shut_down)
    signal.signal(signal.SIGINT, shut_down)
    notifier.start()
    scheduler.start()
    log.info("dashboard %s listening on http://%s as %s", __version__, cfg["listen"], cfg["public_host"])
    try:
        server.serve_forever()
    finally:
        scheduler.stop()
        notifier.stop()
        sessions.close()
        history.close()
        server.server_close()
        log.info("stopped")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m dashboard", description="Server admin dashboard")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the web app (used by systemd)").set_defaults(func=cmd_serve)
    sub.add_parser("set-password", help="set the login username and password").set_defaults(func=cmd_set_password)
    sub.add_parser("test-telegram", help="send a test Telegram message").set_defaults(func=cmd_test_telegram)
    p = sub.add_parser("check", help="run collectors once and print JSON")
    p.add_argument("only", nargs="*", help="collector names (default: all)")
    p.add_argument("-s", "--summary", action="store_true", help="one line per section instead of JSON")
    p.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
