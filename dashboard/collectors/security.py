"""fail2ban, login history, SSH authentication attempts and pending updates."""

import os
import re
import struct
import time
from collections import Counter, deque

from ..util import CommandError, LogTail, parse_syslog_line, read_text, run

DAY = 86400


class Fail2ban:
    cadence = "medium"

    @staticmethod
    def _fields(text: str) -> dict:
        # Lines look like "|  |- Currently failed:\t3" or "`- Banned IP list:\t1.2.3.4 5.6.7.8"
        out = {}
        for line in text.splitlines():
            key, sep, value = line.lstrip(" |`-").partition(":")
            if sep:
                out[key.strip()] = value.strip()
        return out

    def collect(self, cfg: dict) -> dict:
        try:
            overview = self._fields(run(["fail2ban-client", "status"]))
        except CommandError as e:
            return {"error": str(e)}
        jails = []
        for name in filter(None, (j.strip() for j in overview.get("Jail list", "").split(","))):
            f = self._fields(run(["fail2ban-client", "status", name]))
            jails.append({
                "name": name,
                "currently_failed": int(f.get("Currently failed", 0)),
                "total_failed": int(f.get("Total failed", 0)),
                "currently_banned": int(f.get("Currently banned", 0)),
                "total_banned": int(f.get("Total banned", 0)),
                "banned_ips": f.get("Banned IP list", "").split(),
            })
        return {"jails": jails, "banned_total": sum(j["currently_banned"] for j in jails)}


# glibc struct utmp on x86_64: 384 bytes.
UTMP = struct.Struct("<h2xi32s4s32s256s2hi2i16s20s")
USER_PROCESS, DEAD_PROCESS, BOOT_TIME = 7, 8, 2


def _cstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


def read_wtmp(path: str = "/var/log/wtmp", limit: int = 40) -> list[dict]:
    """Newest-first login and reboot records, with session end where wtmp has it."""
    with open(path, "rb") as f:
        data = f.read()
    records = []
    open_lines = {}  # tty line → index into records, for pairing logouts
    for off in range(0, len(data) - UTMP.size + 1, UTMP.size):
        ut_type, pid, line, _id, user, host, _e1, _e2, _sess, sec, _usec, _addr, _ = UTMP.unpack_from(data, off)
        line = _cstr(line)
        if ut_type == USER_PROCESS:
            open_lines[line] = len(records)
            records.append({"type": "login", "user": _cstr(user), "from": _cstr(host), "tty": line,
                            "time": sec, "end": None})
        elif ut_type == DEAD_PROCESS and line in open_lines:
            records[open_lines.pop(line)]["end"] = sec
        elif ut_type == BOOT_TIME:
            records.append({"type": "reboot", "user": "", "from": _cstr(host), "tty": "", "time": sec, "end": None})
    return records[::-1][:limit]


def _sessions() -> list[dict]:
    """Current sessions from logind (/run/utmp no longer exists on this system)."""
    ids = [line.split()[0] for line in run(["loginctl", "list-sessions", "--no-legend"]).splitlines() if line.strip()]
    if not ids:
        return []
    out = run(["loginctl", "show-session", "-p", "Id", "-p", "Name", "-p", "Remote", "-p", "RemoteHost",
               "-p", "Service", "-p", "Class", "-p", "State", "-p", "TimestampMonotonic", *ids])
    mono_offset = time.time() - time.monotonic()
    sessions = []
    for block in out.strip().split("\n\n"):
        p = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if p.get("Class") != "user":
            continue
        started = p.get("TimestampMonotonic", "")
        sessions.append({
            "id": p.get("Id"), "user": p.get("Name"), "service": p.get("Service"),
            "from": p.get("RemoteHost") or ("local" if p.get("Remote") == "no" else ""),
            "state": p.get("State"),
            "since": round(mono_offset + int(started) / 1e6) if started.isdigit() and int(started) else None,
        })
    sessions.sort(key=lambda s: s["since"] or 0, reverse=True)
    return sessions


class Logins:
    cadence = "medium"

    def collect(self, cfg: dict) -> dict:
        out = {}
        try:
            out["history"] = read_wtmp()
        except OSError as e:
            out["history_error"] = e.strerror
        try:
            out["sessions"] = _sessions()
        except CommandError as e:
            out["sessions_error"] = str(e)
        return out


# OpenSSH 9.8+ logs authentication from "sshd-session" rather than "sshd".
_SSHD = re.compile(r"^\S+\s+sshd(?:-session)?\[\d+\]:\s+(.*)$")
_SSH_EVENTS = (
    ("accepted", re.compile(r"^Accepted (\S+) for (\S+) from (\S+) port")),
    ("failed", re.compile(r"^Failed (\S+) for (?:invalid user )?(\S+) from (\S+) port")),
    ("invalid_user", re.compile(r"^Invalid user (\S*) from (\S+) port")),
)


class SshAuth:
    """Rolling 24h window of SSH auth events parsed from /var/log/auth.log."""

    cadence = "medium"
    MAX_EVENTS = 100_000

    def __init__(self, path: str = "/var/log/auth.log"):
        self._tail = LogTail(path)
        self._events = deque(maxlen=self.MAX_EVENTS)  # (ts, kind, user, ip, method)

    def _parse(self, line: str):
        parsed = parse_syslog_line(line)
        if not parsed:
            return None
        ts, rest = parsed
        m = _SSHD.match(rest)
        if not m:
            return None
        msg = m.group(1)
        for kind, rx in _SSH_EVENTS:
            e = rx.match(msg)
            if e:
                if kind == "invalid_user":
                    user, ip = e.groups()
                    return ts, kind, user, ip, ""
                method, user, ip = e.groups()
                return ts, kind, user, ip, method
        return None

    def collect(self, cfg: dict) -> dict:
        try:
            lines = self._tail.read_new()
        except OSError as e:
            return {"error": f"{self._tail.path}: {e.strerror}"}
        for line in lines:
            ev = self._parse(line)
            if ev:
                self._events.append(ev)
        cutoff = time.time() - DAY
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

        counts = Counter(e[1] for e in self._events)
        bad = [e for e in self._events if e[1] != "accepted"]
        return {
            "window_s": DAY,
            "accepted": counts["accepted"],
            "failed": counts["failed"],
            "invalid_user": counts["invalid_user"],
            "top_ips": [{"ip": ip, "attempts": n} for ip, n in Counter(e[3] for e in bad).most_common(10)],
            "top_users": [{"user": u, "attempts": n} for u, n in Counter(e[2] for e in bad).most_common(10)],
            "recent_accepted": [{"time": round(e[0]), "user": e[2], "ip": e[3], "method": e[4]}
                                for e in reversed(self._events) if e[1] == "accepted"][:15],
            "recent_failed": [{"time": round(e[0]), "kind": e[1], "user": e[2], "ip": e[3]}
                              for e in reversed(bad)][:25],
        }


_APT_LINE = re.compile(r"^(\S+)/(\S+)\s+(\S+)\s+\S+\s+\[upgradable from: ([^\]]+)\]")


def _mtime(path: str) -> int | None:
    try:
        return round(os.stat(path).st_mtime)
    except OSError:
        return None


def _last_unattended_run(path: str = "/var/log/unattended-upgrades/unattended-upgrades.log") -> dict:
    """Timestamp and last summary line of the most recent unattended-upgrades run."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.fstat(f.fileno()).st_size - 16384))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError as e:
        return {"error": e.strerror}
    for line in reversed(tail):
        # "2026-10-06 06:25:13,123 INFO No packages found that can be upgraded unattended..."
        m = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (\w+) (.*)$", line)
        if m:
            return {"time": round(time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))),
                    "level": m.group(2), "message": m.group(3)[:200]}
    return {}


class Updates:
    cadence = "slow"

    def collect(self, cfg: dict) -> dict:
        packages = []
        for line in run(["apt", "list", "--upgradable"], timeout=60).splitlines():
            m = _APT_LINE.match(line)
            if m:
                name, suite, new, old = m.groups()
                packages.append({"name": name, "suite": suite, "version": new, "from": old,
                                 "security": "-security" in suite})
        reboot_pkgs = []
        if os.path.exists("/var/run/reboot-required.pkgs"):
            reboot_pkgs = sorted(set(read_text("/var/run/reboot-required.pkgs").split()))
        return {
            "count": len(packages),
            "security_count": sum(p["security"] for p in packages),
            "packages": packages,
            "reboot_required": os.path.exists("/var/run/reboot-required"),
            "reboot_required_since": _mtime("/var/run/reboot-required"),
            "reboot_packages": reboot_pkgs,
            "last_apt_update": _mtime("/var/lib/apt/periodic/update-success-stamp"),
            "last_unattended": _last_unattended_run(),
        }
