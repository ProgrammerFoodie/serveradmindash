"""Host identity, uptime and time sync."""

import os
import platform
import socket
import time

from ..util import CommandError, read_text, run


def _os_release() -> dict:
    out = {}
    for line in read_text("/etc/os-release").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key] = value.strip().strip('"')
    return out


def _cpu_model() -> str:
    for line in read_text("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            return line.partition(":")[2].strip()
    return platform.processor() or "unknown"


def _timezone() -> str:
    try:
        target = os.readlink("/etc/localtime")
        return target.split("zoneinfo/", 1)[-1]
    except OSError:
        return time.tzname[0]


def _virtualization() -> str:
    try:
        return run(["systemd-detect-virt"]).strip()
    except CommandError:
        # Exits 1 and prints "none" on bare metal.
        return "none"


def _time_sync() -> dict:
    """Parse `chronyc -n tracking`; offsets are reported in seconds."""
    try:
        out = run(["chronyc", "-n", "tracking"])
    except CommandError as e:
        return {"error": str(e)}
    fields = {}
    for line in out.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    offset = fields.get("System time", "")
    parts = offset.split()
    offset_s = None
    if parts:
        try:
            offset_s = float(parts[0]) * (-1 if "slow" in offset else 1)
        except ValueError:
            pass
    return {
        "source": fields.get("Reference ID", ""),
        "stratum": int(fields["Stratum"]) if fields.get("Stratum", "").isdigit() else None,
        "offset_s": offset_s,
        "leap_status": fields.get("Leap status", ""),
        "synced": fields.get("Leap status") == "Normal",
    }


def collect(cfg: dict) -> dict:
    uptime_s = float(read_text("/proc/uptime").split()[0])
    osr = _os_release()
    uname = os.uname()
    return {
        "hostname": socket.gethostname(),
        "os": osr.get("PRETTY_NAME", "Linux"),
        "kernel": uname.release,
        "arch": uname.machine,
        "virtualization": _virtualization(),
        "cpu_model": _cpu_model(),
        "cpu_count": os.cpu_count(),
        "uptime_s": round(uptime_s),
        "boot_time": round(time.time() - uptime_s),
        "timezone": _timezone(),
        "time_sync": _time_sync(),
        "python": platform.python_version(),
    }


class System:
    cadence = "medium"

    def collect(self, cfg: dict) -> dict:
        return collect(cfg)
