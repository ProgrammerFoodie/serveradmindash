"""Alerting: the rules that decide what is wrong right now, and the Telegram sender.

evaluate() is a pure function of the latest collector results. It reports the *instantaneous*
state; phase 6 adds the memory around it (how long a condition lasted, hysteresis, cooldowns,
who has already been told) and the Telegram delivery.
"""

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("dashboard.alerts")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramError(Exception):
    pass


def send_telegram(cfg: dict, text: str, timeout: float = 10.0) -> None:
    """Send an HTML-formatted message. Raises TelegramError without leaking the token."""
    tg = cfg["telegram"]
    if not tg["enabled"]:
        raise TelegramError("telegram.enabled is false")
    if not tg["bot_token"] or not str(tg["chat_id"]):
        raise TelegramError("telegram.bot_token or telegram.chat_id is empty in config.json")

    body = json.dumps({
        "chat_id": tg["chat_id"],
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        TELEGRAM_API.format(token=tg["bot_token"]), data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            reply = json.load(resp)
    except urllib.error.HTTPError as e:
        # Telegram explains the problem in the body, e.g. "chat not found".
        try:
            reason = json.load(e).get("description", e.reason)
        except ValueError:
            reason = e.reason
        finally:
            e.close()
        raise TelegramError(f"HTTP {e.code}: {reason}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TelegramError(f"network error: {getattr(e, 'reason', e)}") from None
    if not reply.get("ok"):
        raise TelegramError(reply.get("description", "unknown error"))


# ---- rules -------------------------------------------------------------------------------------

LEVEL_ORDER = {"crit": 0, "warn": 1, "info": 2}

# An alert that is already active only clears once the value is this far back inside the safe zone,
# so a reading hovering around a threshold does not make it flap on and off.
HYSTERESIS = {"cpu": 3, "load": 0.25, "mem": 3, "swap": 3, "disk": 1, "inodes": 1, "ssl": 1}
UNIT_FOR_S = 30        # a service must stay down this long before it counts (a quick restart is not an outage)

# Which collector section each alert id depends on, so a failed collector holds alerts instead of clearing them.
SECTION_OF = (("cpu", "cpu"), ("load", "cpu"), ("mem", "memory"), ("swap", "memory"), ("oom", "memory"),
              ("disk:", "disks"), ("inodes:", "disks"), ("ssl:", "ssl"), ("unit:", "systemd"), ("failed:", "systemd"),
              ("supervisor:", "supervisor"), ("tailscale", "tailscale"), ("updates:", "updates"), ("reboot", "updates"))


def section_of(alert_id: str) -> str | None:
    return next((section for prefix, section in SECTION_OF if alert_id == prefix or alert_id.startswith(prefix)), None)


def _high(value, rule, was=None, hysteresis=0):
    """Level for a value where bigger is worse; an active alert needs `hysteresis` of slack to drop a level."""
    if value is None:
        return None
    if value >= rule["crit"] or (was == "crit" and value >= rule["crit"] - hysteresis):
        return "crit"
    if value >= rule["warn"] or (was in ("warn", "crit") and value >= rule["warn"] - hysteresis):
        return "warn"
    return None


def _low(value, rule, was=None, hysteresis=0):
    """Level for a value where smaller is worse (for example available memory)."""
    if value is None:
        return None
    if value <= rule["crit"] or (was == "crit" and value <= rule["crit"] + hysteresis):
        return "crit"
    if value <= rule["warn"] or (was in ("warn", "crit") and value <= rule["warn"] + hysteresis):
        return "warn"
    return None


def _rules_cpu(th, cpu, add, was):
    busy = cpu["total"]["busy"] if cpu.get("total") else None
    add("cpu", _high(busy, th["cpu_pct"], was("cpu"), HYSTERESIS["cpu"]), f"CPU busy {busy}%", "", th["cpu_pct"].get("for_s", 0))
    add("load", _high(cpu.get("load_per_core"), th["load_per_core"], was("load"), HYSTERESIS["load"]),
        f"Load {cpu.get('load_per_core')} per core", f"load average {cpu['load'][0]} on {cpu['cores']} core(s)",
        th["load_per_core"].get("for_s", 0))


def _rules_memory(th, mem, add, was):
    add("mem", _low(mem["available_pct"], th["mem_avail_pct"], was("mem"), HYSTERESIS["mem"]),
        f"Only {mem['available_pct']}% of RAM available", "", th["mem_avail_pct"].get("for_s", 0))
    add("swap", _high(mem["swap_used_pct"], th["swap_pct"], was("swap"), HYSTERESIS["swap"]),
        f"Swap {mem['swap_used_pct']}% used", "", th["swap_pct"].get("for_s", 0))
    if mem.get("oom_kills_new"):
        add("oom", "crit", "The kernel killed a process (out of memory)", f"{mem['oom_kills_total']} OOM kills since boot")


def _rules_disks(th, disks, add, was):
    for m in disks["mounts"]:
        if "error" in m:
            continue
        add(f"disk:{m['mount']}", _high(m["used_pct"], th["disk_pct"], was(f"disk:{m['mount']}"), HYSTERESIS["disk"]),
            f"Disk {m['mount']} {m['used_pct']}% full")
        add(f"inodes:{m['mount']}", _high(m["inodes_pct"], th["inode_pct"], was(f"inodes:{m['mount']}"), HYSTERESIS["inodes"]),
            f"Inodes on {m['mount']} {m['inodes_pct']}% used")


def _rules_ssl(th, ssl, add, was):
    for c in ssl["certificates"]:
        add(f"ssl:{c['name']}", _low(c.get("days_left"), th["ssl_days"], was(f"ssl:{c['name']}"), HYSTERESIS["ssl"]),
            f"Certificate {c['name']} expires in {c.get('days_left')} days")


def _rules_systemd(th, systemd, add, was):
    watched = set()
    for u in systemd["watched"]:
        watched.add(u["unit"])
        # Only units that are meant to run: disabled or not-yet-installed units stay quiet.
        if u["exists"] and u["enabled"] == "enabled" and u["active"] != "active":
            broken = u["active"] == "failed" or u["result"] not in ("success", "")
            add(f"unit:{u['name']}", "crit" if broken else "warn", f"{u['name']} is {u['active']}",
                "stopped cleanly (on purpose?)" if not broken else f"result: {u['result']}", UNIT_FOR_S)
    for f in systemd["failed"]:
        if f["unit"] not in watched:
            add(f"failed:{f['unit']}", "warn", f"{f['unit']} failed", f.get("description", ""), UNIT_FOR_S)


def _rules_supervisor(th, sup, add, was):
    for p in sup["programs"]:
        if p["state"] != "RUNNING":
            add(f"supervisor:{p['name']}", "crit", f"{p['name']} is {p['state']}", p.get("detail", ""), UNIT_FOR_S)


def _rules_tailscale(th, ts, add, was):
    if ts["state"] != "Running":
        add("tailscale", "crit", f"Tailscale is {ts['state']}", "", UNIT_FOR_S)


def _rules_updates(th, upd, add, was):
    if upd["security_count"]:
        add("updates:security", "warn", f"{upd['security_count']} security update(s) pending")
    if upd["reboot_required"]:
        add("reboot", "info", "Reboot required", ", ".join(upd["reboot_packages"][:5]))


RULES = (("cpu", _rules_cpu), ("memory", _rules_memory), ("disks", _rules_disks), ("ssl", _rules_ssl),
         ("systemd", _rules_systemd), ("supervisor", _rules_supervisor), ("tailscale", _rules_tailscale),
         ("updates", _rules_updates))


def evaluate(cfg: dict, sections: dict, active: dict | None = None, skipped: set | None = None) -> list[dict]:
    """What is wrong right now, worst first.

    active:  {alert id: level} of alerts that are currently confirmed; they get hysteresis.
    skipped: if given, it is filled with the names of sections that produced no data (missing,
             failed or of an unexpected shape), so the caller does not mistake "unknown" for "fine".

    Each rule runs on its own, so one bad collector can never break the alert list (or the
    API response that carries it).
    """
    th = cfg["thresholds"]
    active = active or {}
    alerts: list[dict] = []

    def add(alert_id, level, title, detail="", for_s=0):
        if level:
            alerts.append({"id": alert_id, "level": level, "title": title, "detail": detail, "for_s": for_s})

    for name, rule in RULES:
        data = sections.get(name)
        if not isinstance(data, dict) or "error" in data:
            if skipped is not None:
                skipped.add(name)
            continue
        try:
            rule(th, data, add, active.get)
        except (KeyError, TypeError, IndexError, ValueError, ZeroDivisionError):
            log.warning("alert rule for %s skipped: unexpected data shape", name, exc_info=True)
            if skipped is not None:
                skipped.add(name)
    alerts.sort(key=lambda a: (LEVEL_ORDER[a["level"]], a["id"]))
    return alerts
