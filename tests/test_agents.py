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

    # ---- hostile trees (the collector runs as root over folders a normal user controls) ----

    def test_a_meta_file_that_is_a_fifo_or_a_symlink_to_dev_zero_is_ignored_without_blocking(self):
        self.write("fifo", [msg(self.now - 10, "m1", 5)])
        os.mkfifo(self.sub / "agent-fifo.meta.json")
        self.write("zero", [msg(self.now - 10, "m1", 5)])
        os.symlink("/dev/zero", self.sub / "agent-zero.meta.json")
        rep = self.agents.collect({})                       # must return, not hang or exhaust memory
        self.assertEqual(rep["agents"]["unknown"]["runs"], 2)

    def test_an_oversized_meta_file_is_not_read(self):
        self.write("big", [msg(self.now - 10, "m1", 5)])
        (self.sub / "agent-big.meta.json").write_text(json.dumps({"agentType": "Evil", "pad": "x" * (mod.MAX_META_BYTES + 10)}))
        self.assertIn("unknown", self.agents.collect({})["agents"])

    def test_a_transcript_swapped_for_a_symlink_is_not_followed(self):
        outside = Path(self.tmp.name) / "secret.jsonl"
        outside.write_text(json.dumps(msg(self.now - 10, "m1", 5)) + "\n")
        link = self.sub / "agent-swap.jsonl"
        os.symlink(outside, link)
        self.assertIsNone(read_run(str(link)))              # O_NOFOLLOW, even if the earlier lstat check was raced

    def test_symlinked_directories_are_neither_followed_nor_looped_over(self):
        os.symlink(".", self.root / "proj" / "loop-a")
        os.symlink(".", self.root / "proj" / "loop-b")
        outside = Path(self.tmp.name) / "elsewhere" / "sess" / "subagents"
        outside.mkdir(parents=True)
        (outside / "agent-x.jsonl").write_text(json.dumps(msg(self.now - 10, "m1", 5)) + "\n")
        os.symlink(Path(self.tmp.name) / "elsewhere", self.root / "linked-project")
        self.write("real", [msg(self.now - 10, "m1", 5)], {"agentType": "Explore"})
        rep = self.agents.collect({})
        self.assertEqual(rep["totals"]["runs"], 1)
        self.assertEqual(rep["scanned"], 1)

    def test_a_huge_line_is_skipped_and_the_rest_of_the_file_still_counts(self):
        big = '{"timestamp": "%s", "pad": "%s"}' % (iso(self.now - 50), "x" * (mod.MAX_LINE_BYTES + 5))
        p = self.sub / "agent-long.jsonl"
        p.write_text(big + "\n" + json.dumps(msg(self.now - 10, "m1", 7)) + "\n")
        r = read_run(str(p))
        self.assertEqual(r["out"], 7)
        self.assertAlmostEqual(r["end"] - r["start"], 0, places=2)      # the long line's timestamp was never parsed

    def test_files_over_the_size_limit_are_not_parsed(self):
        p = self.write("a", [msg(self.now - 10, "m1", 5)])
        old, mod.MAX_FILE_BYTES = mod.MAX_FILE_BYTES, 10
        try:
            self.assertIsNone(read_run(str(p)))
        finally:
            mod.MAX_FILE_BYTES = old

    def test_the_same_agent_id_in_two_projects_gives_two_runs(self):
        other = self.root / "proj2" / "sess" / "subagents"
        other.mkdir(parents=True)
        for d in (self.sub, other):
            (d / "agent-same.jsonl").write_text(json.dumps(msg(self.now - 10, "m1", 5)) + "\n")
        self.assertEqual(self.agents.collect({})["totals"]["runs"], 2)

    def test_an_unusable_file_is_not_opened_again_until_it_changes(self):
        p = self.write("empty", None, raw="")
        calls = []
        real = mod.read_run
        mod.read_run = lambda path: calls.append(path) or real(path)
        try:
            self.agents.collect({}); self.agents.collect({})
            self.assertEqual(len(calls), 1)
            p.write_text(json.dumps(msg(self.now - 10, "m1", 5)) + "\n")
            os.utime(p, (self.now + 9, self.now + 9))
            self.assertEqual(self.agents.collect({})["totals"]["runs"], 1)
        finally:
            mod.read_run = real

    def test_messages_without_any_id_are_not_merged_into_one(self):
        lines = [{"type": "assistant", "timestamp": iso(self.now - 10 + i),
                  "message": {"usage": {"output_tokens": 10}}} for i in range(3)]
        self.assertEqual(read_run(str(self.write("noid", lines)))["out"], 30)

    def test_one_collect_reads_at_most_a_bounded_number_of_files(self):
        for i in range(5):
            self.write(f"f{i}", [msg(self.now - 10, "m1", 5)])
        old, mod.SCAN_FILES = mod.SCAN_FILES, 2
        try:
            self.assertEqual(self.agents.collect({})["totals"]["runs"], 2)
            self.assertEqual(self.agents.collect({})["totals"]["runs"], 4)
            self.assertEqual(self.agents.collect({})["totals"]["runs"], 5)
        finally:
            mod.SCAN_FILES = old

    def test_days_follow_the_clock_across_a_dst_change(self):
        old = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Riga"; time.tzset()
        try:
            d = datetime(2026, 10, 25, 12, 0)                           # EU summer time ends 2026-10-25 04:00 -> 03:00 local
            now = d.timestamp()
            a = Agents(roots=[str(self.root)], db_path=Path(self.tmp.name) / "dst.db")
            start = datetime(2026, 10, 25, 0, 0).timestamp()
            self.write("dst", [msg(start, "m1", 5), msg(start + 25 * 3600 - 1, "m2", 5)], {"agentType": "Explore"})
            real_time, mod.time.time = mod.time.time, lambda: now
            try:
                rep = a.collect({})
            finally:
                mod.time.time = real_time
            day = rep["days"].index("2026-10-25")
            self.assertAlmostEqual(rep["agents"]["Explore"]["seconds"][day], 25 * 3600 - 1, delta=1)     # the 25-hour day is counted whole
            self.assertEqual(rep["agents"]["Explore"]["seconds"][day - 1], 0)
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()


if __name__ == "__main__":
    unittest.main()
