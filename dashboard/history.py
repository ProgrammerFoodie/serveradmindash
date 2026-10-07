"""SQLite history: 5 s samples for 24 h, 1 min rollups for 7 d, 15 min rollups for 90 d.

Storage is narrow (one row per metric per timestamp) so per-device and per-interface
metrics need no schema changes. Tables use WITHOUT ROWID with primary key (metric, ts),
which keeps rows small and makes every query a primary-key range seek.

Writes are buffered in memory and committed once per flush() so the disk is touched
about once a minute. Rollups only ever process complete buckets and are idempotent.
"""

import math
import os
import sqlite3
import threading
import time
from pathlib import Path

# (table, resolution in seconds, retention in seconds), finest first.
TIERS = (
    ("s5", 5, 24 * 3600),
    ("m1", 60, 7 * 24 * 3600),
    ("m15", 900, 90 * 24 * 3600),
)
EVENT_RETENTION = 90 * 24 * 3600
MAX_POINTS = 600           # points returned per series
MAX_BUFFER = 2000          # samples kept in memory if the database is unwritable (~3 h)

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS s5  (m INTEGER NOT NULL, ts INTEGER NOT NULL, v REAL NOT NULL,
                                PRIMARY KEY (m, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS m1  (m INTEGER NOT NULL, ts INTEGER NOT NULL, avg REAL NOT NULL, max REAL NOT NULL,
                                PRIMARY KEY (m, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS m15 (m INTEGER NOT NULL, ts INTEGER NOT NULL, avg REAL NOT NULL, max REAL NOT NULL,
                                PRIMARY KEY (m, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, kind TEXT NOT NULL,
                                   subject TEXT NOT NULL, severity TEXT NOT NULL, message TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
"""


def extract_metrics(snap: dict) -> dict[str, float]:
    """Flatten collector output into {metric name: number}.

    Sections that failed (they contain "error") or have no rate yet (None) are skipped,
    so a broken collector leaves a gap in its own charts and nowhere else.
    """
    out: dict[str, float] = {}

    def put(name, value):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            out[name] = value

    def ok(name):
        section = snap.get(name)
        return section if isinstance(section, dict) and "error" not in section else None

    if cpu := ok("cpu"):
        t = cpu.get("total")
        if t:
            put("cpu.busy", t["busy"])
            put("cpu.user", t["user"] + t["nice"])
            put("cpu.system", t["system"] + t["irq"] + t["softirq"])
            put("cpu.iowait", t["iowait"])
            put("cpu.steal", t["steal"])
        put("load.1", cpu["load"][0])
        put("load.5", cpu["load"][1])
        put("psi.cpu_some", cpu["pressure"]["some"]["avg10"])

    if mem := ok("memory"):
        for key in ("used_pct", "available", "cached", "swap_used_pct", "swap_used", "swap_in_Bps",
                    "swap_out_Bps", "major_faults_per_s"):
            put(f"mem.{key}", mem.get(key))
        put("psi.mem_some", mem["pressure"]["some"]["avg10"])
        put("psi.mem_full", mem["pressure"]["full"]["avg10"])

    if io := ok("disk_io"):
        for d in io["devices"]:
            for key in ("read_Bps", "write_Bps", "busy_pct", "await_ms"):
                put(f"io.{d['device']}.{key}", d.get(key))
        put("psi.io_some", io["pressure"]["some"]["avg10"])
        put("psi.io_full", io["pressure"]["full"]["avg10"])

    if net := ok("net_io"):
        for i in net["interfaces"]:
            for key in ("rx_Bps", "tx_Bps"):
                put(f"net.{i['name']}.{key}", i.get(key))

    if disks := ok("disks"):
        for m in disks["mounts"]:
            put(f"fs.{m['mount']}.used_pct", m.get("used_pct"))   # inodes change too slowly to chart
    return out


class History:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()       # guards the write connection, the buffer and _ids
        self._buffer: list[tuple[int, dict]] = []
        self._ids: dict[str, int] = {}
        # Create the file private first: SQLite gives the -wal and -shm files the main file's mode.
        os.close(os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600))
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=10, isolation_level=None)
        # auto_vacuum only takes effect before the first table exists; it lets incremental_vacuum
        # hand freed pages back to the filesystem instead of the file never shrinking.
        self._db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        os.chmod(self.path, 0o600)      # a database that already existed may have looser permissions
        self._ids = dict(self._db.execute("SELECT name, id FROM metrics"))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- writing -------------------------------------------------------------------------

    def record(self, ts: float, metrics: dict[str, float]) -> None:
        """Buffer one sample (nothing is written until flush())."""
        with self._lock:
            self._buffer.append((int(ts), metrics))
            del self._buffer[:-MAX_BUFFER]

    def _metric_id(self, name: str) -> int:
        mid = self._ids.get(name)
        if mid is None:
            self._db.execute("INSERT OR IGNORE INTO metrics (name) VALUES (?)", (name,))
            mid = self._db.execute("SELECT id FROM metrics WHERE name=?", (name,)).fetchone()[0]
            self._ids[name] = mid
        return mid

    def flush(self) -> int:
        """Write all buffered samples in one transaction; returns the number of rows written."""
        with self._lock:
            batch, self._buffer = self._buffer, []
            if not batch:
                return 0
            try:
                self._db.execute("BEGIN IMMEDIATE")
                rows = [(self._metric_id(name), ts, value)
                        for ts, metrics in batch for name, value in metrics.items()]
                self._db.executemany("INSERT OR REPLACE INTO s5 (m, ts, v) VALUES (?, ?, ?)", rows)
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                self._buffer = (batch + self._buffer)[-MAX_BUFFER:]   # keep the samples for the next try
                raise
            return len(rows)

    def add_event(self, kind: str, subject: str, severity: str, message: str, ts: float | None = None) -> None:
        with self._lock:
            self._db.execute("INSERT INTO events (ts, kind, subject, severity, message) VALUES (?, ?, ?, ?, ?)",
                             (int(ts or time.time()), kind, subject, severity, message[:500]))

    # ---- maintenance ---------------------------------------------------------------------

    def _rollup(self, src: str, dst: str, res: int, now: int) -> int:
        """Aggregate complete `res`-second buckets of src into dst. Safe to repeat."""
        agg = "AVG(v), MAX(v)" if src == "s5" else "AVG(avg), MAX(max)"
        end = now // res * res                                   # only buckets that are over
        last = self._db.execute(f"SELECT MAX(ts) FROM {dst}").fetchone()[0]
        if last is None:
            first = self._db.execute(f"SELECT MIN(ts) FROM {src}").fetchone()[0]
            if first is None:
                return 0
            start = first // res * res
        else:
            start = last                                         # recompute the newest bucket, harmless
        if start >= end:
            return 0
        written = 0
        self._db.execute("BEGIN IMMEDIATE")
        try:
            for (mid,) in self._db.execute("SELECT id FROM metrics").fetchall():
                cur = self._db.execute(
                    f"INSERT OR REPLACE INTO {dst} (m, ts, avg, max) "
                    f"SELECT m, ts / ? * ?, {agg} FROM {src} WHERE m = ? AND ts >= ? AND ts < ? "
                    f"GROUP BY ts / ?", (res, res, mid, start, end, res))
                written += cur.rowcount
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        return written

    def maintain(self, now: float | None = None, vacuum: bool = False) -> dict:
        """Roll up complete buckets, delete expired rows, optionally return free pages to the OS."""
        now = int(now or time.time())
        with self._lock:
            result = {
                "m1": self._rollup("s5", "m1", 60, now),
                "m15": self._rollup("m1", "m15", 900, now),
                "deleted": 0,
            }
            # Delete per metric: the primary key is (metric, ts), so a bare `ts < ?` would scan the table.
            for (mid,) in self._db.execute("SELECT id FROM metrics").fetchall():
                for table, _res, keep in TIERS:
                    result["deleted"] += self._db.execute(
                        f"DELETE FROM {table} WHERE m = ? AND ts < ?", (mid, now - keep)).rowcount
            result["deleted"] += self._db.execute("DELETE FROM events WHERE ts < ?",
                                                  (now - EVENT_RETENTION,)).rowcount
            if vacuum:
                self._db.execute("PRAGMA incremental_vacuum")
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return result

    # ---- reading -------------------------------------------------------------------------

    def _reader(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA query_only=ON")
        return db

    def metric_names(self) -> list[str]:
        db = self._reader()
        try:
            return sorted(name for (name,) in db.execute("SELECT name FROM metrics"))
        finally:
            db.close()

    def query(self, metrics: list[str], range_s: int, end: float | None = None) -> dict:
        """Series for the last `range_s` seconds as [timestamp, average, maximum] points.

        Points are averaged into buckets of `step_s` seconds so at most about MAX_POINTS come
        back per series; the tier is chosen to read as few rows as that resolution allows.
        """
        end = int(end or time.time())
        range_s = max(60, min(int(range_s), TIERS[-1][2]))
        wanted = math.ceil(range_s / MAX_POINTS)                 # seconds per point we are aiming for
        holds = [t for t in TIERS if t[2] >= range_s]
        # Coarsest tier that is still fine enough (fewest rows to read); else the finest that holds the range.
        table, res, _ = next((t for t in reversed(holds) if t[1] <= wanted), holds[0])
        step = max(res, math.ceil(wanted / res) * res)
        cols = "AVG(v), MAX(v)" if table == "s5" else "AVG(avg), MAX(max)"
        series = {}
        db = self._reader()
        try:
            for name in metrics:
                row = db.execute("SELECT id FROM metrics WHERE name=?", (name,)).fetchone()
                points = []
                if row:
                    for t, avg, mx in db.execute(
                            f"SELECT ts / ? * ?, {cols} FROM {table} WHERE m = ? AND ts > ? AND ts <= ? "
                            f"GROUP BY ts / ? ORDER BY 1", (step, step, row[0], end - range_s, end, step)):
                        points.append([t, round(avg, 3), round(mx, 3)])
                series[name] = points
        finally:
            db.close()
        return {"range_s": range_s, "step_s": step, "tier": table, "end": end, "series": series}

    def events(self, since: float | None = None, limit: int = 200, kinds: tuple | None = None) -> list[dict]:
        sql, args = "SELECT ts, kind, subject, severity, message FROM events WHERE ts >= ?", [int(since or 0)]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        db = self._reader()
        try:
            rows = db.execute(sql + " ORDER BY ts DESC LIMIT ?", args + [limit]).fetchall()
        finally:
            db.close()
        return [{"ts": r[0], "kind": r[1], "subject": r[2], "severity": r[3], "message": r[4]} for r in rows]

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += Path(self.path + suffix).stat().st_size
            except OSError:
                pass
        return total
