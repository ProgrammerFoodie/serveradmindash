"""Filesystem usage/inodes per mount and IO throughput per block device."""

import os

from ..util import Delta, pressure, rate, read_int, read_text

REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "exfat", "f2fs", "zfs", "ntfs3"}
SECTOR = 512  # /proc/diskstats always counts 512-byte sectors


def _mounts() -> list[tuple[str, str, str]]:
    seen, out = set(), []
    for line in read_text("/proc/mounts").splitlines():
        dev, mnt, fstype, *_ = line.split()
        mnt = mnt.replace("\\040", " ")
        if fstype in REAL_FS and dev.startswith("/dev/") and dev not in seen:
            seen.add(dev)
            out.append((dev, mnt, fstype))
    return out


def _roles() -> dict[str, str]:
    """Map block device name → what it holds (mount point or "swap")."""
    roles = {os.path.basename(dev): mnt for dev, mnt, _ in _mounts()}
    for line in read_text("/proc/swaps").splitlines()[1:]:
        name = line.split()[0]
        if name.startswith("/dev/"):
            roles[os.path.basename(name)] = "swap"
    return roles


def parse_readonly(mountinfo: str) -> dict[str, bool]:
    """{mount point: read-only?} from /proc/<pid>/mountinfo text; the last mount at a point wins."""
    out = {}
    for line in mountinfo.splitlines():
        left, _, right = line.partition(" - ")
        fields, tail = left.split(), right.split()
        if len(fields) < 6 or len(tail) < 3:
            continue
        point = fields[4].replace("\\040", " ")
        options = set(fields[5].split(",")) | set(tail[2].split(","))
        out[point] = "ro" in options
    return out


def _readonly_map() -> dict[str, bool]:
    """Read-only state as the host sees it. Our own namespace is no use: systemd's ProtectSystem=
    mounts everything read-only for this service, so statvfs would report every disk as read-only."""
    for path in ("/proc/1/mountinfo", "/proc/self/mountinfo"):
        try:
            return parse_readonly(read_text(path))
        except OSError:
            continue
    return {}


def _pct(used: int, total: int) -> float:
    return round(100 * used / total, 1) if total else 0.0


class DiskUsage:
    cadence = "medium"

    def collect(self, cfg: dict) -> dict:
        mounts = []
        read_only = _readonly_map()
        for dev, mnt, fstype in _mounts():
            try:
                st = os.statvfs(mnt)
            except OSError as e:
                mounts.append({"device": dev, "mount": mnt, "fstype": fstype, "error": e.strerror})
                continue
            size = st.f_blocks * st.f_frsize
            free = st.f_bfree * st.f_frsize
            avail = st.f_bavail * st.f_frsize
            used = size - free
            inodes_used = st.f_files - st.f_ffree
            mounts.append({
                "device": dev, "mount": mnt, "fstype": fstype,
                "size": size, "used": used, "available": avail,
                # Same formula as df: reserved blocks count as unavailable.
                "used_pct": _pct(used, used + avail),
                "inodes_total": st.f_files, "inodes_used": inodes_used,
                "inodes_pct": _pct(inodes_used, st.f_files),
                "readonly": read_only.get(mnt, bool(st.f_flag & os.ST_RDONLY)),
            })
        return {"mounts": mounts}


def _diskstats() -> dict:
    devices = {d for d in os.listdir("/sys/block") if not d.startswith(("loop", "ram", "zram"))}
    out = {}
    for line in read_text("/proc/diskstats").splitlines():
        f = line.split()
        if f[2] in devices:
            out[f[2]] = {
                "reads": int(f[3]), "read_sectors": int(f[5]), "read_ms": int(f[6]),
                "writes": int(f[7]), "write_sectors": int(f[9]), "write_ms": int(f[10]),
                "in_flight": int(f[11]), "io_ms": int(f[12]),
            }
    return out


class DiskIO:
    cadence = "fast"

    def __init__(self):
        self._delta = Delta()

    def collect(self, cfg: dict) -> dict:
        stats = _diskstats()
        prev, elapsed = self._delta.update(stats)
        roles = _roles()
        devices = []
        for name, s in sorted(stats.items()):
            sectors = read_int(f"/sys/block/{name}/size")
            size = sectors * SECTOR if sectors is not None else None
            d = {"device": name, "role": roles.get(name, ""), "size": size, "in_flight": s["in_flight"],
                 "read_Bps": None, "write_Bps": None, "read_iops": None, "write_iops": None,
                 "busy_pct": None, "await_ms": None}
            p = prev.get(name) if prev else None
            if p:
                d["read_Bps"] = round(rate(s["read_sectors"], p["read_sectors"], elapsed) * SECTOR)
                d["write_Bps"] = round(rate(s["write_sectors"], p["write_sectors"], elapsed) * SECTOR)
                d["read_iops"] = round(rate(s["reads"], p["reads"], elapsed), 1)
                d["write_iops"] = round(rate(s["writes"], p["writes"], elapsed), 1)
                d["busy_pct"] = round(min(100.0, rate(s["io_ms"], p["io_ms"], elapsed) / 10), 1)
                ios = (s["reads"] - p["reads"]) + (s["writes"] - p["writes"])
                wait = (s["read_ms"] - p["read_ms"]) + (s["write_ms"] - p["write_ms"])
                d["await_ms"] = round(wait / ios, 2) if ios > 0 else 0.0
            devices.append(d)
        return {"devices": devices, "pressure": pressure("io")}
