"""Sub-agent usage: tokens and run time per agent type per day, from Claude Code's sub-agent transcripts.

Each transcript (`<projects>/<project>/<session>/subagents/agent-<id>.jsonl`, with a `.meta.json` next to it
naming the agent type) becomes one row in agents.db. Rows are kept after Claude Code deletes the transcript,
so the 30-day history survives its cleanup. Files are re-read only when their modification time changes.
Only the agent type and a short task title (from .meta.json) ever leave this module, never transcript text.
"""

import glob
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta

from .. import config

DAYS = 30
KEEP_DAYS = 400
MAX_FILE_BYTES = 50 * 1024 * 1024
RECENT = 20
DESCR_MAX = 120
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def parse_ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def default_roots() -> list[str]:
    return sorted(glob.glob("/home/*/.claude/projects")) + ["/root/.claude/projects"]


def read_run(path: str) -> dict | None:
    """One transcript as a run, or None if it has no usable timestamps."""
    msgs: dict = {}
    first = last = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    ts = parse_ts(d["timestamp"])
                except (ValueError, KeyError, TypeError):
                    continue
                first = ts if first is None else min(first, ts)
                last = ts if last is None else max(last, ts)
                m = d.get("message")
                if d.get("type") == "assistant" and isinstance(m, dict) and isinstance(m.get("usage"), dict):
                    cur = msgs.setdefault(m.get("id") or d.get("uuid"), [0, 0, 0, 0, m.get("model")])
                    # streamed chunks repeat one message id: keep the largest value of each field
                    for i, k in enumerate(TOKEN_FIELDS):
                        v = m["usage"].get(k)
                        if isinstance(v, int) and not isinstance(v, bool):
                            cur[i] = max(cur[i], v)
    except OSError:
        return None
    if first is None:
        return None
    meta = {}
    try:
        with open(path[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                meta = loaded
    except (OSError, ValueError):
        pass
    tot = [sum(v[i] for v in msgs.values()) for i in range(4)]
    return {"id": os.path.basename(path)[:-len(".jsonl")],
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
        return c

    def _scan(self, c: sqlite3.Connection, now: float) -> int:
        known = dict(c.execute("SELECT id, mtime FROM runs"))
        seen = 0
        for root in (self._roots if self._roots is not None else default_roots()):
            for p in glob.glob(os.path.join(glob.escape(root), "*", "**", "subagents", "agent-*.jsonl"), recursive=True):
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if not os.path.isfile(p) or os.path.islink(p) or st.st_size > MAX_FILE_BYTES:
                    continue
                seen += 1
                if known.get(os.path.basename(p)[:-len(".jsonl")]) == st.st_mtime:
                    continue
                r = read_run(p)
                if r:
                    c.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                              (r["id"], r["agent"], r["descr"], r["model"], r["start"], r["end"],
                               r["inp"], r["out"], r["cread"], r["cwrite"], st.st_mtime))
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
        tz = datetime.fromtimestamp(now).astimezone().tzinfo
        today = datetime.fromtimestamp(now, tz).replace(hour=0, minute=0, second=0, microsecond=0)
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
        return {"days": [d.strftime("%Y-%m-%d") for d in days], "timezone": datetime.fromtimestamp(now, tz).strftime("%Z"),
                "agents": agents, "recent": recent, "scanned": scanned,
                "totals": {"runs": sum(a["runs"] for a in agents.values()),
                           "tokens": sum(sum(a["tokens"]) for a in agents.values()),
                           "seconds": round(sum(sum(a["seconds"]) for a in agents.values()), 1)}}
