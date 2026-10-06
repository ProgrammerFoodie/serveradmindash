"""Watched systemd units, failed units, timers and supervisor programs."""

import json
import re
import time

from ..util import CommandError, read_text, run

SHOW_PROPS = ("Id", "Description", "LoadState", "ActiveState", "SubState", "Result", "UnitFileState",
              "MainPID", "NRestarts", "MemoryCurrent", "CPUUsageNSec", "ActiveEnterTimestamp")


def _int_or_none(value: str) -> int | None:
    return int(value) if value.isdigit() else None   # "[not set]" and "" become None


def _unit(name: str) -> str:
    return name if "." in name else f"{name}.service"


def show_units(names: list[str]) -> list[dict]:
    """Batch `systemctl show` for several units in one call."""
    if not names:
        return []
    out = run(["systemctl", "show", "--timestamp=unix", "-p", ",".join(SHOW_PROPS),
               *[_unit(n) for n in names]])
    now = time.time()
    units = []
    for block in out.strip().split("\n\n"):
        p = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        since = p.get("ActiveEnterTimestamp", "").lstrip("@")
        since = int(since) if since.isdigit() else None
        active = p.get("ActiveState") == "active"
        units.append({
            "name": p.get("Id", "").removesuffix(".service"),
            "unit": p.get("Id", ""),
            "description": p.get("Description", ""),
            "exists": p.get("LoadState") != "not-found",
            "load": p.get("LoadState", ""),
            "active": p.get("ActiveState", ""),
            "sub": p.get("SubState", ""),
            "result": p.get("Result", ""),
            "enabled": p.get("UnitFileState", ""),
            "pid": _int_or_none(p.get("MainPID", "")) or None,
            "restarts": _int_or_none(p.get("NRestarts", "")),
            "memory": _int_or_none(p.get("MemoryCurrent", "")),
            "cpu_time_s": round(int(p["CPUUsageNSec"]) / 1e9, 1) if p.get("CPUUsageNSec", "").isdigit() else None,
            "since": since,
            "uptime_s": round(now - since) if active and since else None,
        })
    return units


class Systemd:
    cadence = "medium"

    def collect(self, cfg: dict) -> dict:
        watched = show_units(cfg["watch"]["systemd"])
        protected = set(cfg["watch"]["protected"])
        for u in watched:
            u["protected"] = u["name"] in protected

        failed = json.loads(run(["systemctl", "list-units", "--failed", "--all", "-o", "json", "--no-pager"]) or "[]")

        timers = []
        for t in json.loads(run(["systemctl", "list-timers", "--all", "-o", "json", "--no-pager"]) or "[]"):
            timers.append({
                "unit": t.get("unit"),
                "activates": t.get("activates"),
                "next": round(t["next"] / 1e6) if t.get("next") else None,
                "last": round(t["last"] / 1e6) if t.get("last") else None,
            })
        timers.sort(key=lambda t: t["next"] or float("inf"))
        return {
            "watched": watched,
            "failed": [{"unit": f["unit"], "description": f.get("description", ""), "sub": f.get("sub", "")}
                       for f in failed],
            "timers": timers,
        }


# supervisorctl status lines, e.g.
#   queue-worker:site-worker_00   RUNNING   pid 1234, uptime 3 days, 1:02:03
# (names carry a group prefix when process_name differs from the program name)
#   queue-worker    FATAL     Exited too quickly (process log may have details)
_SUP_LINE = re.compile(r"^(\S+)\s+([A-Z]+)\s*(.*)$")
_SUP_RUNNING = re.compile(r"pid (\d+), uptime (?:(\d+) days?, )?(\d+):(\d\d):(\d\d)")


def supervisor_status() -> list[dict]:
    # supervisorctl exits 3 when any program is not RUNNING; that is still valid output.
    out = run(["supervisorctl", "status"], ok_codes=(0, 3))
    programs = []
    for line in out.splitlines():
        m = _SUP_LINE.match(line.strip())
        if not m:
            continue
        name, state, detail = m.groups()
        prog = {"name": name, "state": state, "detail": detail, "pid": None, "uptime_s": None, "rss": None}
        r = _SUP_RUNNING.search(detail)
        if r:
            pid, days, h, mnt, s = r.groups()
            prog["pid"] = int(pid)
            prog["uptime_s"] = int(days or 0) * 86400 + int(h) * 3600 + int(mnt) * 60 + int(s)
            prog["detail"] = ""
            try:
                for st in read_text(f"/proc/{pid}/status").splitlines():
                    if st.startswith("VmRSS:"):
                        prog["rss"] = int(st.split()[1]) * 1024
            except OSError:
                pass
        programs.append(prog)
    return programs


class Supervisor:
    cadence = "medium"

    def collect(self, cfg: dict) -> dict:
        try:
            programs = supervisor_status()
        except CommandError as e:
            return {"error": str(e)}
        wanted = cfg["watch"]["supervisor"]
        if wanted != "auto":
            programs = [p for p in programs if p["name"] in wanted]
        return {"programs": programs}
