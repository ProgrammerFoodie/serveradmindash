"""Small helpers shared by collectors: safe reads, external commands, log tailing."""

import os
import re
import subprocess
import time
from datetime import datetime

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


class CommandError(Exception):
    pass


def read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def read_int(path: str, default: int | None = None) -> int | None:
    try:
        return int(read_text(path).strip())
    except (OSError, ValueError):
        return default


def run(args: list[str], timeout: float = 5.0, ok_codes: tuple[int, ...] = (0,), stdin: str | None = None, secret: str | None = None) -> str:
    """Run a command (never through a shell) and return stdout.

    `stdin` is fed to the command's standard input (how passwords reach chpasswd: never on a command line, where
    every user could read them). If `secret` is given, any trace of it is removed from an error message.
    Raises CommandError on a missing binary, timeout or an exit code not in ok_codes.
    """
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False, input=stdin)
    except FileNotFoundError:
        raise CommandError(f"{args[0]}: not installed") from None
    except subprocess.TimeoutExpired:
        raise CommandError(f"{args[0]}: timed out after {timeout}s") from None
    if proc.returncode not in ok_codes:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        detail = lines[-1] if lines else "no output"
        if secret:
            detail = detail.replace(secret, "***")
        raise CommandError(f"{args[0]}: exit {proc.returncode}: {detail}")
    return proc.stdout


def pressure(resource: str) -> dict:
    """Kernel PSI for cpu/memory/io: {"some": {"avg10": .., "avg60": .., "avg300": ..}, "full": {..}}."""
    out = {}
    try:
        text = read_text(f"/proc/pressure/{resource}")
    except OSError:
        return out                                                  # kernel without PSI: only the pressure fields go missing
    for line in text.splitlines():
        kind, *pairs = line.split()
        values = dict(p.split("=") for p in pairs)
        out[kind] = {k: float(values[k]) for k in ("avg10", "avg60", "avg300")}
    return out


def rate(new: float, old: float, seconds: float) -> float:
    """Per-second rate of a monotonically increasing counter; 0 if it wrapped or reset."""
    if seconds <= 0 or new < old:
        return 0.0
    return (new - old) / seconds


class Delta:
    """Remembers the previous sample of counters so collectors can report rates.

    update() returns the seconds elapsed since the previous sample, or None on
    the first call (no rates yet).
    """

    def __init__(self):
        self.prev = None
        self._t = None

    def update(self, sample):
        now = time.monotonic()
        prev, elapsed = self.prev, (now - self._t if self._t is not None else None)
        self.prev, self._t = sample, now
        return prev, elapsed


class LogTail:
    """Incrementally read new complete lines from a log file, surviving logrotate.

    On the first read it backfills from the rotated file (path + ".1", if plain
    text) and the current file. Afterwards only appended bytes are read. When
    the inode changes, the rest of the old file is read from its rotated name.
    """

    MAX_READ = 8 * 1024 * 1024  # never pull more than this into memory at once

    def __init__(self, path: str, backfill_rotated: bool = True):
        self.path = path
        self.rotated = path + ".1"
        self.backfill_rotated = backfill_rotated
        self._ino = None
        self._offset = 0
        self._partial = b""

    def _read_from(self, path: str, offset: int) -> tuple[bytes, int]:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if size < offset:          # truncated in place
                offset = 0
            if size - offset > self.MAX_READ:
                offset = size - self.MAX_READ
            f.seek(offset)
            data = f.read(size - offset)
        return data, offset + len(data)

    def read_new(self) -> list[str]:
        st = os.stat(self.path)
        chunks = []
        if self._ino is None:
            if self.backfill_rotated:
                try:
                    chunks.append(self._read_from(self.rotated, 0)[0])
                except OSError:
                    pass
        elif st.st_ino != self._ino:
            try:
                if os.stat(self.rotated).st_ino == self._ino:
                    chunks.append(self._partial + self._read_from(self.rotated, self._offset)[0])
                    self._partial = b""
            except OSError:
                pass
            self._offset = 0
        data, self._offset = self._read_from(self.path, self._offset)
        self._ino = st.st_ino
        chunks.append(self._partial + data)

        lines = []
        for i, chunk in enumerate(chunks):
            parts = chunk.split(b"\n")
            if i == len(chunks) - 1:
                self._partial = parts.pop()  # incomplete last line waits for the next read
            elif parts and parts[-1] == b"":
                parts.pop()
            lines.extend(p.decode("utf-8", "replace") for p in parts if p)
        return lines


_ISO_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:[+-]\d\d:\d\d|Z))\s+")
_BSD_TS = re.compile(r"^([A-Z][a-z]{2}\s+\d{1,2}\s\d\d:\d\d:\d\d)\s+")


def parse_syslog_line(line: str) -> tuple[float, str] | None:
    """Split a syslog line into (unix time, rest). Handles RFC3339 and classic BSD stamps."""
    m = _ISO_TS.match(line)
    if m:
        try:
            return datetime.fromisoformat(m.group(1)).timestamp(), line[m.end():]
        except ValueError:
            return None
    m = _BSD_TS.match(line)
    if m:
        # BSD stamps carry no zone or year: they are local time, current year.
        now = datetime.now()
        try:
            dt = datetime.strptime(f"{now.year} {m.group(1)}", "%Y %b %d %H:%M:%S")
        except ValueError:
            return None
        if dt > now:               # a December line read in January
            dt = dt.replace(year=now.year - 1)
        return dt.timestamp(), line[m.end():]
    return None
