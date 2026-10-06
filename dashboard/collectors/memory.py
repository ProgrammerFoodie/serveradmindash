"""RAM, swap, swap activity, OOM kills and memory pressure."""

from ..util import PAGE_SIZE, Delta, pressure, rate, read_text


def _meminfo() -> dict:
    out = {}
    for line in read_text("/proc/meminfo").splitlines():
        key, _, value = line.partition(":")
        parts = value.split()
        if parts:
            out[key] = int(parts[0]) * (1024 if len(parts) > 1 else 1)  # kB → bytes
    return out


def _vmstat() -> dict:
    wanted = ("pswpin", "pswpout", "pgmajfault", "oom_kill")
    out = {}
    for line in read_text("/proc/vmstat").splitlines():
        key, value = line.split()
        if key in wanted:
            out[key] = int(value)
    return out


def _swaps() -> list[dict]:
    out = []
    for line in read_text("/proc/swaps").splitlines()[1:]:
        name, kind, size_kb, used_kb, prio = line.split()
        size, used = int(size_kb) * 1024, int(used_kb) * 1024
        out.append({"name": name, "type": kind, "size": size, "used": used,
                    "used_pct": round(100 * used / size, 1) if size else 0.0, "priority": int(prio)})
    return out


def _pct(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


class Memory:
    cadence = "fast"

    def __init__(self):
        self._delta = Delta()

    def collect(self, cfg: dict) -> dict:
        m = _meminfo()
        vm = _vmstat()
        prev, elapsed = self._delta.update(vm)

        total, avail = m["MemTotal"], m["MemAvailable"]
        swap_total, swap_free = m["SwapTotal"], m["SwapFree"]
        out = {
            "total": total,
            "available": avail,
            "available_pct": _pct(avail, total),
            "used": total - avail,
            "used_pct": _pct(total - avail, total),
            "free": m["MemFree"],
            "buffers": m["Buffers"],
            "cached": m["Cached"],
            "slab_reclaimable": m.get("SReclaimable", 0),
            "slab_unreclaimable": m.get("SUnreclaim", 0),
            "anon": m.get("AnonPages", 0),
            "shmem": m.get("Shmem", 0),
            "dirty": m.get("Dirty", 0),
            "writeback": m.get("Writeback", 0),
            "committed": m.get("Committed_AS", 0),
            "commit_limit": m.get("CommitLimit", 0),
            "swap_total": swap_total,
            "swap_used": swap_total - swap_free,
            "swap_used_pct": _pct(swap_total - swap_free, swap_total),
            "swap_cached": m.get("SwapCached", 0),
            "swaps": _swaps(),
            "oom_kills_total": vm.get("oom_kill", 0),
            "pressure": pressure("memory"),
            "swap_in_Bps": None, "swap_out_Bps": None, "major_faults_per_s": None, "oom_kills_new": 0,
        }
        if prev is not None:
            out["swap_in_Bps"] = round(rate(vm["pswpin"], prev["pswpin"], elapsed) * PAGE_SIZE)
            out["swap_out_Bps"] = round(rate(vm["pswpout"], prev["pswpout"], elapsed) * PAGE_SIZE)
            out["major_faults_per_s"] = round(rate(vm["pgmajfault"], prev["pgmajfault"], elapsed), 1)
            out["oom_kills_new"] = max(vm.get("oom_kill", 0) - prev.get("oom_kill", 0), 0)
        return out
