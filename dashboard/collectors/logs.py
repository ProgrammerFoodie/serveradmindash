"""Journal warnings/errors, nginx traffic and errors, SSL expiry, per-unit log viewer."""

import glob
import json
import os
import re
import time
from collections import Counter, deque
from datetime import datetime

from ..util import CommandError, LogTail, run

DAY = 86400
MSG_MAX = 300


def _message(value) -> str:
    # journald stores non-UTF-8 messages as a list of byte values.
    if isinstance(value, list):
        value = bytes(value).decode("utf-8", "replace")
    return (value or "").strip()[:MSG_MAX]


_UFW_FIELD = re.compile(r"\b(SRC|DPT|PROTO)=(\S+)")


def firewall_block(message: str) -> dict | None:
    """Fields of a UFW block line, e.g. "[UFW BLOCK] IN=eth0 ... SRC=1.2.3.4 ... PROTO=TCP ... DPT=22"."""
    if "[UFW BLOCK]" not in message:
        return None
    return dict(_UFW_FIELD.findall(message))


class Journal:
    """Rolling 24h window of priority ≤ warning entries, read incrementally via cursor.

    Firewall block lines (thousands a day on an internet-facing host) are counted and summarised
    separately, so they cannot bury the messages that need a person.
    """

    cadence = "medium"
    MAX_ENTRIES = 20_000

    def __init__(self):
        self._cursor = None
        self._entries = deque(maxlen=self.MAX_ENTRIES)  # (ts, priority, unit, message)

    def collect(self, cfg: dict) -> dict:
        args = ["journalctl", "-p", "warning", "-o", "json", "--no-pager", "-q",
                "--output-fields=PRIORITY,_SYSTEMD_UNIT,SYSLOG_IDENTIFIER,MESSAGE"]
        if self._cursor:
            args.append(f"--after-cursor={self._cursor}")
        else:
            args += ["--since=-24h", f"-n{self.MAX_ENTRIES}"]
        try:
            out = run(args, timeout=20)
        except CommandError as e:
            return {"error": str(e)}
        for line in out.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            self._cursor = e.get("__CURSOR", self._cursor)
            unit = e.get("_SYSTEMD_UNIT") or e.get("SYSLOG_IDENTIFIER") or "kernel"
            self._entries.append((int(e["__REALTIME_TIMESTAMP"]) / 1e6, int(e.get("PRIORITY", 4)),
                                  unit.removesuffix(".service"), _message(e.get("MESSAGE"))))

        cutoff = time.time() - DAY
        while self._entries and self._entries[0][0] < cutoff:
            self._entries.popleft()

        by_unit, blocks, sources, ports, others = {}, 0, Counter(), Counter(), []
        for entry in self._entries:
            ts, prio, unit, msg = entry
            fw = firewall_block(msg)
            if fw is not None:
                blocks += 1
                sources[fw.get("SRC", "?")] += 1
                ports[f"{fw.get('PROTO', '?')}/{fw.get('DPT', '?')}"] += 1
                continue
            others.append(entry)
            u = by_unit.setdefault(unit, {"unit": unit, "errors": 0, "warnings": 0, "last": 0, "last_message": ""})
            u["errors" if prio <= 3 else "warnings"] += 1
            if ts >= u["last"]:
                u["last"], u["last_message"] = round(ts), msg
        units = sorted(by_unit.values(), key=lambda u: (u["errors"], u["warnings"]), reverse=True)
        return {
            "window_s": DAY,
            "errors": sum(u["errors"] for u in units),
            "warnings": sum(u["warnings"] for u in units),
            "units": units,
            "recent": [{"time": round(ts), "priority": p, "unit": u, "message": m}
                       for ts, p, u, m in others[-100:][::-1]],
            "firewall": {"blocks": blocks,
                         "top_sources": [{"ip": ip, "count": n} for ip, n in sources.most_common(10)],
                         "top_ports": [{"port": port, "count": n} for port, n in ports.most_common(10)]},
        }


# nginx "combined" format.
_COMBINED = re.compile(
    r'^(\S+) \S+ (\S+) \[([^\]]+)\] "([^"]*)" (\d{3}) (\d+|-) "([^"]*)" "([^"]*)"')
_TOP_KEEP = 300  # per-hour counter cap, keeps memory bounded under scanner floods


class _Hour:
    __slots__ = ("requests", "bytes", "status", "paths", "ips", "agents")

    def __init__(self):
        self.requests = self.bytes = 0
        self.status = Counter()
        self.paths, self.ips, self.agents = Counter(), Counter(), Counter()

    def prune(self):
        for c in (self.paths, self.ips, self.agents):
            if len(c) > _TOP_KEEP * 2:
                keep = c.most_common(_TOP_KEEP)
                c.clear()
                c.update(dict(keep))


class Nginx:
    """Request rate, status classes and top talkers over 24h from the main access log."""

    cadence = "medium"

    def __init__(self, access="/var/log/nginx/access.log", error="/var/log/nginx/error.log"):
        self._tail = LogTail(access)
        self._error_path = error
        self._hours: dict[int, _Hour] = {}
        self._minutes = Counter()          # minute start → requests, last 60 minutes
        self._recent_5xx = deque(maxlen=50)

    def _ingest(self, line: str) -> None:
        m = _COMBINED.match(line)
        if not m:
            return
        ip, _user, ts, request, status, size, _ref, agent = m.groups()
        try:
            t = datetime.strptime(ts, "%d/%b/%Y:%H:%M:%S %z").timestamp()
        except ValueError:
            return
        parts = request.split()
        path = parts[1].split("?", 1)[0] if len(parts) >= 2 else request[:80]
        h = self._hours.setdefault(int(t // 3600 * 3600), _Hour())
        h.requests += 1
        h.bytes += int(size) if size.isdigit() else 0
        h.status[status[0] + "xx"] += 1
        h.paths[path[:200]] += 1
        h.ips[ip] += 1
        h.agents[agent[:120] or "-"] += 1
        self._minutes[int(t // 60 * 60)] += 1
        if status.startswith("5"):
            self._recent_5xx.append({"time": round(t), "status": int(status), "ip": ip, "request": request[:200]})

    def _error_tail(self, lines: int = 30) -> list[str]:
        try:
            with open(self._error_path, "rb") as f:
                f.seek(max(0, os.fstat(f.fileno()).st_size - 32768))
                text = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return []
        return [line[:MSG_MAX] for line in text[-lines:]][::-1]

    def collect(self, cfg: dict) -> dict:
        try:
            lines = self._tail.read_new()
        except OSError as e:
            return {"error": f"{self._tail.path}: {e.strerror}"}
        for line in lines:
            self._ingest(line)

        now = time.time()
        for hour in [h for h in self._hours if h < now - DAY]:
            del self._hours[hour]
        for minute in [m for m in self._minutes if m < now - 3600]:
            del self._minutes[minute]
        for h in self._hours.values():
            h.prune()

        status, paths, ips, agents = Counter(), Counter(), Counter(), Counter()
        for h in self._hours.values():
            status.update(h.status)
            paths.update(h.paths)
            ips.update(h.ips)
            agents.update(h.agents)
        this_minute = int(now // 60 * 60)
        return {
            "window_s": DAY,
            "requests": sum(h.requests for h in self._hours.values()),
            "bytes": sum(h.bytes for h in self._hours.values()),
            "status": dict(sorted(status.items())),
            # Oldest → newest, the last 60 complete minutes.
            "per_minute": [self._minutes.get(this_minute - 60 * i, 0) for i in range(60, 0, -1)],
            "per_hour": [{"hour": k, "requests": h.requests, "errors": h.status["4xx"] + h.status["5xx"]}
                         for k, h in sorted(self._hours.items())],
            "top_paths": [{"path": p, "requests": n} for p, n in paths.most_common(15)],
            "top_ips": [{"ip": ip, "requests": n} for ip, n in ips.most_common(15)],
            "top_agents": [{"agent": a, "requests": n} for a, n in agents.most_common(10)],
            "recent_5xx": list(self._recent_5xx)[::-1],
            "error_log": self._error_tail(),
        }


class Ssl:
    cadence = "slow"

    def collect(self, cfg: dict) -> dict:
        paths = sorted(glob.glob("/etc/letsencrypt/live/*/cert.pem"))
        if not paths and not os.access("/etc/letsencrypt/live", os.R_OK):
            return {"error": "/etc/letsencrypt/live: permission denied"}
        certs = []
        for path in paths:
            name = os.path.basename(os.path.dirname(path))
            try:
                out = run(["openssl", "x509", "-noout", "-enddate", "-issuer", "-ext", "subjectAltName",
                           "-in", path])
            except CommandError as e:
                certs.append({"name": name, "error": str(e)})
                continue
            end = re.search(r"notAfter=(.+)", out).group(1).strip()
            expires = datetime.strptime(" ".join(end.split()), "%b %d %H:%M:%S %Y %Z").timestamp()
            issuer = re.search(r"issuer=.*?O\s*=\s*([^,\n]+)", out)
            certs.append({
                "name": name,
                "domains": re.findall(r"DNS:([^,\s]+)", out),
                "issuer": issuer.group(1).strip() if issuer else "",
                "expires": round(expires),
                "days_left": round((expires - time.time()) / DAY, 1),
            })
        certs.sort(key=lambda c: c.get("days_left", -1))
        return {"certificates": certs}


def unit_logs(kind: str, name: str, lines: int = 200) -> list[str]:
    """Last log lines of a systemd unit or supervisor program, for the on-demand viewer.

    The caller must check that `name` is an allowed (watched) unit or program.
    """
    lines = max(10, min(lines, 1000))
    if kind == "systemd":
        out = run(["journalctl", "-u", name if "." in name else f"{name}.service",
                   "-n", str(lines), "-o", "short-iso", "--no-pager", "-q"], timeout=15)
    elif kind == "supervisor":
        # supervisorctl tail takes a byte count; ~160 bytes per line is a fair estimate.
        out = run(["supervisorctl", "tail", f"-{lines * 160}", name], timeout=15)
    else:
        raise ValueError(f"unknown log kind {kind!r}")
    return out.splitlines()[-lines:]
