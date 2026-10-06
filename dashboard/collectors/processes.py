"""Per-process table from /proc and per-service totals from cgroup v2."""

import os
import pwd
import time
from functools import lru_cache

from ..util import CLK_TCK, PAGE_SIZE, Delta, rate, read_int, read_text

CMD_MAX = 400
CGROUP_ROOT = "/sys/fs/cgroup"


@lru_cache(maxsize=256)
def _username(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _boot_time() -> float:
    return time.time() - float(read_text("/proc/uptime").split()[0])


KERNEL_THREAD = "kernel"
PF_KTHREAD = 0x00200000     # task flag in /proc/<pid>/stat: this is a kernel thread


def unit_of(cgroup_text: str) -> str:
    """The systemd unit a process belongs to, from the text of /proc/<pid>/cgroup.

    "0::/system.slice/nginx.service"                      -> "nginx.service"
    "0::/user.slice/user-0.slice/session-34.scope"        -> "session-34.scope"
    The first .service in the path wins, so children of a service count as that service.
    """
    parts = [p for line in cgroup_text.splitlines() for p in line.rpartition(":")[2].split("/") if p]
    for part in parts:
        if part.endswith(".service"):
            return part
    for part in reversed(parts):
        if part.endswith(".scope"):
            return part
    return ""



def _read_proc(pid: str) -> dict | str | None:
    """One process; KERNEL_THREAD for kernel threads; None if it vanished mid-read."""
    base = f"/proc/{pid}"
    try:
        stat = read_text(f"{base}/stat")
        status = read_text(f"{base}/status")
        with open(f"{base}/cmdline", "rb") as f:
            cmdline = f.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None

    # comm is in parentheses and may itself contain spaces or ")".
    lparen, rparen = stat.index("("), stat.rindex(")")
    comm = stat[lparen + 1:rparen]
    f = stat[rparen + 2:].split()  # f[0] is field 3 (state)
    ppid = int(f[1])
    if ppid == 2 or int(pid) == 2:
        return KERNEL_THREAD

    st = {}
    for line in status.splitlines():
        key, _, value = line.partition(":")
        if key in ("Uid", "VmSwap", "VmRSS", "Threads"):
            st[key] = value.split()

    io = {}
    try:
        for line in read_text(f"{base}/io").splitlines():
            key, _, value = line.partition(": ")
            if key in ("read_bytes", "write_bytes"):
                io[key] = int(value)
    except (OSError, ValueError):
        pass  # other users' io is root-only

    try:
        unit = unit_of(read_text(f"{base}/cgroup"))
    except OSError:
        unit = ""
    return {
        "pid": int(pid),
        "ppid": ppid,
        "flags": int(f[6]),                          # field 9 of the stat line
        "unit": unit,
        "name": comm,
        "state": f[0],
        "user": _username(int(st["Uid"][0])) if "Uid" in st else "?",
        "ticks": int(f[11]) + int(f[12]),            # utime + stime
        "start_ticks": int(f[19]),
        "threads": int(st["Threads"][0]) if "Threads" in st else int(f[17]),
        "rss": int(st["VmRSS"][0]) * 1024 if "VmRSS" in st else int(f[21]) * PAGE_SIZE,
        "swap": int(st["VmSwap"][0]) * 1024 if "VmSwap" in st else 0,
        "io": io,
        "cmd": (cmdline.replace(b"\0", b" ").decode("utf-8", "replace").strip() or f"[{comm}]")[:CMD_MAX],
    }


class Processes:
    """Full process table. Scheduled only while someone has the Processes tab open."""

    cadence = "fast"

    def __init__(self):
        self._delta = Delta()

    def collect(self, cfg: dict) -> dict:
        procs = {}
        kernel_threads = 0
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            p = _read_proc(pid)
            if p is KERNEL_THREAD:
                kernel_threads += 1
            if not isinstance(p, dict):
                continue
            procs[p["pid"]] = p

        prev, elapsed = self._delta.update(
            {pid: (p["start_ticks"], p["ticks"], p["io"]) for pid, p in procs.items()})
        boot = _boot_time()
        states, rows = {}, []
        for pid, p in procs.items():
            states[p["state"]] = states.get(p["state"], 0) + 1
            row = {k: p[k] for k in ("pid", "ppid", "name", "state", "user", "threads", "rss", "swap", "cmd", "unit")}
            row["start_ticks"] = p["start_ticks"]            # exact identity of this process (a PID can be reused)
            row["start_time"] = round(boot + p["start_ticks"] / CLK_TCK)
            row["cpu_time_s"] = round(p["ticks"] / CLK_TCK, 1)
            row["cpu_pct"] = row["read_Bps"] = row["write_Bps"] = None
            old = prev.get(pid) if prev else None
            if old and old[0] == p["start_ticks"]:  # same process, not a reused PID
                row["cpu_pct"] = round(100 * rate(p["ticks"], old[1], elapsed) / CLK_TCK, 1)
                if p["io"] and old[2]:
                    row["read_Bps"] = round(rate(p["io"]["read_bytes"], old[2]["read_bytes"], elapsed))
                    row["write_Bps"] = round(rate(p["io"]["write_bytes"], old[2]["write_bytes"], elapsed))
            rows.append(row)

        rows.sort(key=lambda r: (r["cpu_pct"] or 0, r["rss"]), reverse=True)
        return {
            "count": len(rows),
            "kernel_threads": kernel_threads,
            "threads_total": sum(r["threads"] for r in rows),
            "states": states,
            "zombies": [r["pid"] for r in rows if r["state"] == "Z"],
            "processes": rows,
        }


def _cgroup_stats(path: str) -> dict:
    usage = None
    try:
        for line in read_text(f"{path}/cpu.stat").splitlines():
            if line.startswith("usage_usec "):
                usage = int(line.split()[1])
                break
    except OSError:
        pass
    return {
        "memory": read_int(f"{path}/memory.current"),
        "swap": read_int(f"{path}/memory.swap.current"),
        "pids": read_int(f"{path}/pids.current"),
        "cpu_usec": usage,
    }


class Cgroups:
    """CPU %, RAM, swap and task count for every systemd service, plus user sessions."""

    cadence = "fast"

    def __init__(self):
        self._delta = Delta()

    def collect(self, cfg: dict) -> dict:
        groups = {}
        slice_dir = f"{CGROUP_ROOT}/system.slice"
        for entry in os.listdir(slice_dir):
            if entry.endswith(".service"):
                groups[entry.removesuffix(".service")] = _cgroup_stats(f"{slice_dir}/{entry}")
        groups["user sessions"] = _cgroup_stats(f"{CGROUP_ROOT}/user.slice")

        prev, elapsed = self._delta.update({k: v["cpu_usec"] for k, v in groups.items()})
        rows = []
        for name, g in groups.items():
            cpu_pct = None
            if prev and prev.get(name) is not None and g["cpu_usec"] is not None:
                cpu_pct = round(100 * rate(g["cpu_usec"], prev[name], elapsed) / 1e6, 1)
            rows.append({"name": name, "cpu_pct": cpu_pct, "memory": g["memory"],
                         "swap": g["swap"], "pids": g["pids"]})
        rows.sort(key=lambda r: (r["memory"] or 0) + (r["swap"] or 0), reverse=True)
        return {"services": rows}
