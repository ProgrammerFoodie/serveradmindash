"""Sub-agent usage: tokens and run time per agent type per day, from Claude Code's sub-agent transcripts.

Each transcript (`<projects>/<project>/<session>/subagents/agent-<id>.jsonl`, with a `.meta.json` next to it
naming the agent type) becomes one row in agents.db. Rows are kept after Claude Code deletes the transcript,
so the 30-day history survives its cleanup. Files are re-read only when their modification time changes.
Only the agent type and a short task title (from .meta.json) ever leave this module, never transcript text.
"""

import json
import os
import sqlite3
import stat
import time
from datetime import datetime, timedelta

from .. import config

DAYS = 30
KEEP_DAYS = 400
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024            # a longer line (a pasted image, a huge tool result) is skipped, never parsed
MAX_META_BYTES = 64 * 1024
SCAN_FILES = 200                        # work per collect(): the rest waits for the next minute, so one pass can never hog the thread
SCAN_BYTES = 200 * 1024 * 1024
RECENT = 20
DESCR_MAX = 120
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def parse_ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def default_roots() -> list[str]:
    """The projects folder of every home, but never through a symlink (`.claude` or `projects` could point anywhere)."""
    roots = []
    try:
        homes = sorted(os.scandir("/home"), key=lambda e: e.name)
    except OSError:
        homes = []
    for home in [e.path for e in homes if e.is_dir(follow_symlinks=False)] + ["/root"]:
        claude, projects = os.path.join(home, ".claude"), os.path.join(home, ".claude", "projects")
        if os.path.isdir(projects) and not os.path.islink(claude) and not os.path.islink(projects):
            roots.append(projects)
    return roots


def open_regular(path: str, limit: int):
    """A binary file object for a plain file of at most `limit` bytes, or None.

    This runs as root on paths a normal user controls, so: never follow a symlink (O_NOFOLLOW), never block on a
    FIFO (O_NONBLOCK), and check what was actually opened (fstat on the descriptor), not what the path looked like
    a moment earlier."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            os.close(fd)
            return None
        return os.fdopen(fd, "rb")
    except OSError:
        os.close(fd)
        return None


def iter_transcripts(root: str):
    """Yield (path, DirEntry stat) for <root>/<project>/<session>/subagents/agent-*.jsonl.

    Exactly three levels, no symlinked directories followed: a link such as `a -> .` cannot make this loop or leave the tree."""
    def subdirs(path):
        try:
            with os.scandir(path) as it:
                return sorted((e for e in it if e.is_dir(follow_symlinks=False)), key=lambda e: e.name)
        except OSError:
            return []
    for project in subdirs(root):
        for session in subdirs(project.path):
            try:
                with os.scandir(os.path.join(session.path, "subagents")) as it:
                    files = sorted((e for e in it if e.name.startswith("agent-") and e.name.endswith(".jsonl")
                                    and e.is_file(follow_symlinks=False)), key=lambda e: e.name)
            except OSError:
                continue
            for e in files:
                yield e.path, e


def read_run(path: str) -> dict | None:
    """One transcript as a run, or None if it is unusable (not a plain file, too big, no timestamps)."""
    f = open_regular(path, MAX_FILE_BYTES)
    if f is None:
        return None
    msgs: dict = {}
    first = last = None
    with f:
        n = 0
        while True:
            line = f.readline(MAX_LINE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_LINE_BYTES and not line.endswith(b"\n"):
                while True:                                   # drain the rest of an over-long line without keeping it
                    rest = f.readline(MAX_LINE_BYTES)
                    if not rest or rest.endswith(b"\n"):
                        break
                continue
            n += 1
            try:
                d = json.loads(line)
                ts = parse_ts(d["timestamp"])
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            first = ts if first is None else min(first, ts)
            last = ts if last is None else max(last, ts)
            m = d.get("message")
            if d.get("type") == "assistant" and isinstance(m, dict) and isinstance(m.get("usage"), dict):
                cur = msgs.setdefault(m.get("id") or d.get("uuid") or f"line{n}", [0, 0, 0, 0, m.get("model")])
                # streamed chunks repeat one message id: keep the largest value of each field
                for i, k in enumerate(TOKEN_FIELDS):
                    v = m["usage"].get(k)
                    if isinstance(v, int) and not isinstance(v, bool):
                        cur[i] = max(cur[i], v)
    if first is None:
        return None
    meta = {}
    mf = open_regular(path[:-len(".jsonl")] + ".meta.json", MAX_META_BYTES)
    if mf is not None:
        with mf:
            try:
                loaded = json.loads(mf.read(MAX_META_BYTES))
                if isinstance(loaded, dict):
                    meta = loaded
            except ValueError:
                pass
    tot = [sum(v[i] for v in msgs.values()) for i in range(4)]
    return {"id": path,
            "agent": str(meta.get("agentType") or "unknown")[:60],
            "descr": str(meta.get("description") or "")[:DESCR_MAX],
            "model": next((v[4] for v in msgs.values() if v[4]), "") or "",
            "start": first, "end": last, "inp": tot[0], "out": tot[1], "cread": tot[2], "cwrite": tot[3]}


class Agents:
    cadence = "medium"

    def __init__(self, roots: list[str] | None = None, db_path=None):
        self._roots = roots
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        path = self._db_path or config.DATA_DIR / "agents.db"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        c = sqlite3.connect(path, timeout=10)
        c.execute("""CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, agent TEXT, descr TEXT, model TEXT,
                     start REAL, end REAL, inp INT, out INT, cread INT, cwrite INT, mtime REAL)""")
        c.execute("CREATE INDEX IF NOT EXISTS runs_end ON runs(end)")
        c.execute("CREATE TABLE IF NOT EXISTS skipped(id TEXT PRIMARY KEY, mtime REAL)")   # unusable files: not opened again until they change
        return c

    def _scan(self, c: sqlite3.Connection, now: float) -> int:
        known = dict(c.execute("SELECT id, mtime FROM runs"))
        known.update(c.execute("SELECT id, mtime FROM skipped"))
        seen = reads = size = 0
        for root in (self._roots if self._roots is not None else default_roots()):
            for path, entry in iter_transcripts(root):
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                seen += 1
                if known.get(path) == st.st_mtime:
                    continue
                if reads >= SCAN_FILES or size + st.st_size > SCAN_BYTES:
                    continue                                       # over this minute's budget: picked up on a later pass
                reads += 1
                size += st.st_size
                r = read_run(path)
                if r:
                    c.execute("DELETE FROM skipped WHERE id=?", (path,))
                    c.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                              (r["id"], r["agent"], r["descr"], r["model"], r["start"], r["end"],
                               r["inp"], r["out"], r["cread"], r["cwrite"], st.st_mtime))
                else:
                    c.execute("INSERT OR REPLACE INTO skipped VALUES(?,?)", (path, st.st_mtime))
        c.execute("DELETE FROM runs WHERE end < ?", (now - KEEP_DAYS * 86400,))
        c.commit()
        return seen

    def collect(self, cfg: dict) -> dict:
        now = time.time()
        c = self._connect()
        try:
            scanned = self._scan(c, now)
            return self._report(c, now, scanned)
        finally:
            c.close()

    @staticmethod
    def _report(c: sqlite3.Connection, now: float, scanned: int) -> dict:
        # Naive local midnights: .timestamp() applies the zone rules of that very day, so a DST change inside the window is right.
        today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        first_day = today - timedelta(days=DAYS - 1)
        days = [first_day + timedelta(days=i) for i in range(DAYS)]
        bounds = [(d.timestamp(), (d + timedelta(days=1)).timestamp()) for d in days]
        agents: dict = {}
        rows = c.execute("SELECT agent,start,end,inp,out,cread,cwrite FROM runs WHERE end>=?", (first_day.timestamp(),)).fetchall()
        for agent, s, e, inp, out, cr, cw in rows:
            a = agents.setdefault(agent, {"runs": 0, "tokens": [0] * DAYS, "output": [0] * DAYS,
                                          "seconds": [0.0] * DAYS, "runs_per_day": [0] * DAYS})
            # tokens and the run count belong to the day the run started; time is split at midnight
            di = next((i for i, (lo, hi) in enumerate(bounds) if lo <= s < hi), None)
            if di is not None:
                a["tokens"][di] += inp + out + cr + cw
                a["output"][di] += inp + out
                a["runs_per_day"][di] += 1
                a["runs"] += 1
            for i, (lo, hi) in enumerate(bounds):
                a["seconds"][i] += max(0.0, min(e, hi) - max(s, lo))
        for a in agents.values():
            a["seconds"] = [round(v, 1) for v in a["seconds"]]
        recent = [{"agent": r[0], "descr": r[1], "model": r[2], "start": r[3], "seconds": round(max(r[4] - r[3], 0.0), 1),
                   "tokens": r[5] + r[6] + r[7] + r[8]}
                  for r in c.execute("SELECT agent,descr,model,start,end,inp,out,cread,cwrite FROM runs ORDER BY start DESC LIMIT ?", (RECENT,))]
        return {"days": [d.strftime("%Y-%m-%d") for d in days], "timezone": time.strftime("%Z", time.localtime(now)),
                "agents": agents, "recent": recent, "scanned": scanned,
                "totals": {"runs": sum(a["runs"] for a in agents.values()),
                           "tokens": sum(sum(a["tokens"]) for a in agents.values()),
                           "seconds": round(sum(sum(a["seconds"]) for a in agents.values()), 1)}}
