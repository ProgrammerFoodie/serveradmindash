"""Append-only record of everything that was done (or refused) through the dashboard.

One JSON object per line in data/audit.jsonl (mode 600), rotated at 1 MB with three older files kept.
A failure to write is logged loudly but never blocks an action: when the disk is full is exactly when
you need to be able to kill a runaway process.
"""

import json
import logging
import os
import threading
import time

log = logging.getLogger("dashboard.audit")

MAX_BYTES = 1_000_000
KEEP = 3


class Audit:
    def __init__(self, path, max_bytes: int = MAX_BYTES):
        self.path = str(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def record(self, user: str, ip: str, action: str, target: str, ok: bool, result: str, detail: str = "") -> dict:
        entry = {"ts": round(time.time(), 3), "user": user[:64], "ip": ip[:64], "action": action[:64],
                 "target": target[:200], "ok": bool(ok), "result": result[:300], "detail": detail[:500]}
        line = (json.dumps(entry, sort_keys=True) + "\n").encode()
        with self._lock:
            try:
                self._rotate_if_needed(len(line))
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(fd, line)
                finally:
                    os.close(fd)
            except OSError:
                log.exception("could not write the audit log: %s", entry)
        return entry

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            if os.stat(self.path).st_size + incoming <= self.max_bytes:
                return
        except FileNotFoundError:
            return
        for n in range(KEEP, 0, -1):                       # audit.jsonl.2 -> .3, .1 -> .2, current -> .1
            src = self.path if n == 1 else f"{self.path}.{n - 1}"
            try:
                os.replace(src, f"{self.path}.{n}")
            except FileNotFoundError:
                pass

    def tail(self, limit: int = 50) -> list[dict]:
        """The newest entries first, reading older rotated files if needed."""
        out: list[dict] = []
        with self._lock:
            for path in [self.path] + [f"{self.path}.{n}" for n in range(1, KEEP + 1)]:
                try:
                    with open(path, encoding="utf-8") as f:
                        lines = f.read().splitlines()
                except OSError:
                    continue
                for line in reversed(lines):
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue                           # a torn line must not hide the rest
                    if len(out) >= limit:
                        return out
        return out
