"""CPU time split, load, scheduler activity and pressure."""

import os

from ..util import Delta, pressure, rate, read_text

# /proc/stat cpu columns. guest/guest_nice are already counted in user/nice.
FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")


def _parse_stat() -> dict:
    cpus, counters = {}, {}
    for line in read_text("/proc/stat").splitlines():
        name, *values = line.split()
        if name.startswith("cpu"):
            cpus[name] = dict(zip(FIELDS, map(int, values)))
        elif name in ("ctxt", "processes", "procs_running", "procs_blocked"):
            counters[name] = int(values[0])
        elif name == "intr":
            counters[name] = int(values[0])
    return {"cpus": cpus, **counters}


def _split(new: dict, old: dict) -> dict:
    """Percent of elapsed CPU time per field, plus busy (everything but idle/iowait)."""
    deltas = {f: max(new[f] - old[f], 0) for f in FIELDS}
    total = sum(deltas.values())
    if not total:
        return {**{f: 0.0 for f in FIELDS}, "busy": 0.0}
    pct = {f: round(100 * d / total, 2) for f, d in deltas.items()}
    pct["busy"] = round(100 - pct["idle"] - pct["iowait"], 2)
    return pct


class Cpu:
    cadence = "fast"

    def __init__(self):
        self._delta = Delta()

    def collect(self, cfg: dict) -> dict:
        stat = _parse_stat()
        prev, elapsed = self._delta.update(stat)
        load1, load5, load15, tasks, _ = read_text("/proc/loadavg").split()
        running, total = map(int, tasks.split("/"))
        cores = os.cpu_count() or 1

        out = {
            "cores": cores,
            "load": [float(load1), float(load5), float(load15)],
            "load_per_core": round(float(load1) / cores, 2),
            "tasks_running": stat["procs_running"],
            "tasks_blocked": stat["procs_blocked"],
            "tasks_total": total,
            "pressure": pressure("cpu"),
            "total": None, "per_core": None,
            "ctxt_per_s": None, "intr_per_s": None, "forks_per_s": None,
        }
        if prev is not None:
            out["total"] = _split(stat["cpus"]["cpu"], prev["cpus"]["cpu"])
            out["per_core"] = [
                _split(stat["cpus"][name], prev["cpus"][name])
                for name in sorted((n for n in stat["cpus"] if n != "cpu"), key=lambda n: int(n[3:]))
                if name in prev["cpus"]
            ]
            out["ctxt_per_s"] = round(rate(stat["ctxt"], prev["ctxt"], elapsed))
            out["intr_per_s"] = round(rate(stat["intr"], prev["intr"], elapsed))
            out["forks_per_s"] = round(rate(stat["processes"], prev["processes"], elapsed), 2)
        return out
