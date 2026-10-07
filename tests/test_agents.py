import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from dashboard.collectors import agents as mod
from dashboard.collectors.agents import Agents, read_run


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z")


def msg(ts, mid, out, inp=1, cread=0, cwrite=0, model="m1"):
    return {"type": "assistant", "timestamp": iso(ts), "uuid": mid + str(out),
            "message": {"id": mid, "model": model, "usage": {"input_tokens": inp, "output_tokens": out,
                                                                "cache_read_input_tokens": cread, "cache_creation_input_tokens": cwrite}}}


class AgentsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "projects"
        self.sub = self.root / "proj" / "sess" / "subagents"
        self.sub.mkdir(parents=True)
        self.agents = Agents(roots=[str(self.root)], db_path=Path(self.tmp.name) / "db" / "agents.db")
        self.now = time.time()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, lines, meta=None, raw=None):
        p = self.sub / f"agent-{name}.jsonl"
        p.write_text(raw if raw is not None else "\n".join(json.dumps(x) for x in lines) + "\n")
        if meta is not None:
            (self.sub / f"agent-{name}.meta.json").write_text(json.dumps(meta))
        return p

    def test_a_repeated_message_id_is_counted_once_with_the_largest_values(self):
        p = self.write("a", [msg(self.now - 100, "m1", 10, cread=500), msg(self.now - 99, "m1", 40, cread=500),
                             msg(self.now - 90, "m2", 5, inp=2, cwrite=7), {"type": "user", "timestamp": iso(self.now - 80)}],
                       {"agentType": "Explore", "description": "x" * 500})
        r = read_run(str(p))
        self.assertEqual((r["inp"], r["out"], r["cread"], r["cwrite"]), (3, 45, 500, 7))
        self.assertEqual((r["agent"], len(r["descr"]), r["model"]), ("Explore", mod.DESCR_MAX, "m1"))
        self.assertAlmostEqual(r["end"] - r["start"], 20, places=2)

    def test_report_sums_tokens_per_agent_and_day(self):
        self.write("a", [msg(self.now - 100, "m1", 10, cread=90)], {"agentType": "Explore"})
        self.write("b", [msg(self.now - 50, "m1", 20)], {"agentType": "Explore"})
        rep = self.agents.collect({})
        self.assertEqual(rep["agents"]["Explore"]["runs"], 2)
        self.assertEqual(sum(rep["agents"]["Explore"]["tokens"]), 101 + 21)
        self.assertEqual(sum(rep["agents"]["Explore"]["output"]), 11 + 21)
        self.assertEqual(rep["totals"]["runs"], 2)
        self.assertEqual(len(rep["days"]), 30)
        self.assertEqual(rep["scanned"], 2)

    def test_a_run_over_midnight_is_split_in_time_but_counted_once(self):
        today = datetime.fromtimestamp(self.now).astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        mid = today.timestamp()
        self.write("a", [msg(mid - 600, "m1", 10), msg(mid + 1800, "m2", 10)], {"agentType": "Explore"})
        a = self.agents.collect({})["agents"]["Explore"]
        self.assertEqual(a["runs"], 1)
        self.assertEqual((a["runs_per_day"][-2], a["runs_per_day"][-1]), (1, 0))      # counted on the start day
        self.assertAlmostEqual(a["seconds"][-2], 600, delta=1)
        self.assertAlmostEqual(a["seconds"][-1], 1800, delta=1)

    def test_unchanged_files_are_not_read_again_but_changed_ones_are(self):
        p = self.write("a", [msg(self.now - 100, "m1", 10)], {"agentType": "Explore"})
        calls = []
        real = mod.read_run
        mod.read_run = lambda path: calls.append(path) or real(path)
        try:
            self.agents.collect({}); self.agents.collect({})
            self.assertEqual(len(calls), 1)
            p.write_text(p.read_text() + json.dumps(msg(self.now - 50, "m2", 30)) + "\n")
            os.utime(p, (self.now + 5, self.now + 5))
            rep = self.agents.collect({})
            self.assertEqual(len(calls), 2)
            self.assertEqual(sum(rep["agents"]["Explore"]["output"]), 11 + 31)
        finally:
            mod.read_run = real

    def test_bad_lines_and_missing_meta_do_not_break_anything(self):
        self.write("a", None, raw='not json\n{"timestamp": "bad"}\n' + json.dumps(msg(self.now - 10, "m1", 5)) + "\n[1,2]\n")
        self.write("empty", None, raw="")
        (self.sub / "agent-b.meta.json").write_text("{broken")
        self.write("b", [msg(self.now - 10, "m1", 5)])
        rep = self.agents.collect({})
        self.assertEqual(rep["agents"]["unknown"]["runs"], 2)

    def test_symlinks_are_ignored(self):
        outside = Path(self.tmp.name) / "outside.jsonl"
        outside.write_text(json.dumps(msg(self.now - 10, "m1", 5)) + "\n")
        os.symlink(outside, self.sub / "agent-link.jsonl")
        self.assertEqual(self.agents.collect({})["totals"]["runs"], 0)

    def test_old_rows_are_pruned_and_rows_outlive_their_transcripts(self):
        old = self.now - (mod.KEEP_DAYS + 5) * 86400
        p1 = self.write("old", [msg(old, "m1", 5)], {"agentType": "Explore"})
        p2 = self.write("new", [msg(self.now - 10, "m1", 5)], {"agentType": "Explore"})
        self.agents.collect({})
        c = self.agents._connect()
        self.assertEqual(c.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
        c.close()
        p2.unlink()
        self.assertEqual(self.agents.collect({})["agents"]["Explore"]["runs"], 1)       # still there without the transcript

    def test_no_roots_is_an_empty_report(self):
        rep = Agents(roots=[], db_path=Path(self.tmp.name) / "e.db").collect({})
        self.assertEqual((rep["agents"], rep["recent"], rep["totals"]["runs"]), ({}, [], 0))

    def test_only_documented_keys_leave_the_collector(self):
        self.write("a", [msg(self.now - 10, "m1", 5)], {"agentType": "Explore", "description": "t"})
        rep = self.agents.collect({})
        self.assertEqual(set(rep), {"days", "timezone", "agents", "recent", "scanned", "totals"})
        self.assertEqual(set(rep["recent"][0]), {"agent", "descr", "model", "start", "seconds", "tokens"})
        self.assertNotIn("proj", json.dumps(rep))                                       # no paths


if __name__ == "__main__":
    unittest.main()
