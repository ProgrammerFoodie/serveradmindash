import os
import tempfile
import unittest

from dashboard import collectors, config
from dashboard.history import MAX_POINTS, History, extract_metrics

T0 = 1_800_000_000  # divisible by 900, so bucket edges are easy to reason about


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.h = History(os.path.join(self.dir.name, "h.db"))

    def tearDown(self):
        self.h.close()
        self.dir.cleanup()

    def fill(self, start, seconds, fn, name="x"):
        for ts in range(start, start + seconds, 5):
            self.h.record(ts, {name: fn(ts)})
        self.h.flush()

    def count(self, table):
        return self.h._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_files_are_private(self):
        self.h.record(T0, {"a": 1.0})
        self.h.flush()
        for name in os.listdir(self.dir.name):
            self.assertEqual(os.stat(os.path.join(self.dir.name, name)).st_mode & 0o077, 0, name)

    def test_auto_vacuum_is_incremental(self):
        self.assertEqual(self.h._db.execute("PRAGMA auto_vacuum").fetchone()[0], 2)

    def test_flush_writes_once_and_replaces_duplicates(self):
        self.h.record(T0, {"a": 1.0, "b": 2.0})
        self.h.record(T0, {"a": 5.0})            # same timestamp: last write wins
        self.assertEqual(self.h.flush(), 3)
        self.assertEqual(self.h.flush(), 0)      # buffer is empty now
        self.assertEqual(self.count("s5"), 2)
        self.assertEqual(self.h.query(["a"], 60, end=T0 + 5)["series"]["a"][0][1], 5.0)

    def test_rollup_avg_max_and_complete_buckets_only(self):
        self.fill(T0, 130, lambda ts: ts - T0)    # values 0,5,...,125
        res = self.h.maintain(now=T0 + 130)
        self.assertEqual(res["m1"], 2)            # minutes [0,60) and [60,120); the third is still open
        rows = self.h._db.execute("SELECT ts, avg, max FROM m1 ORDER BY ts").fetchall()
        self.assertEqual(rows, [(T0, sum(range(0, 60, 5)) / 12, 55.0), (T0 + 60, sum(range(60, 120, 5)) / 12, 115.0)])

    def test_maintain_is_idempotent_and_picks_up_new_buckets(self):
        self.fill(T0, 120, lambda ts: 1.0)
        self.h.maintain(now=T0 + 120)
        before = self.h._db.execute("SELECT * FROM m1 ORDER BY ts").fetchall()
        self.h.maintain(now=T0 + 120)
        self.assertEqual(self.h._db.execute("SELECT * FROM m1 ORDER BY ts").fetchall(), before)
        self.fill(T0 + 120, 60, lambda ts: 3.0)
        self.h.maintain(now=T0 + 180)
        self.assertEqual(self.count("m1"), 3)

    def test_m15_rolls_up_from_m1(self):
        self.fill(T0, 900, lambda ts: 10.0 if ts < T0 + 450 else 20.0)
        self.h.maintain(now=T0 + 900)
        avg, mx = self.h._db.execute("SELECT avg, max FROM m15").fetchone()
        self.assertEqual(mx, 20.0)
        self.assertAlmostEqual(avg, 15.0, delta=1.0)   # 7.5 of 15 minutes at 10, the rest at 20 (boundary minute mixes)

    def test_retention_prunes_each_tier(self):
        self.fill(T0, 60, lambda ts: 1.0)
        self.h.maintain(now=T0 + 60)
        self.assertEqual((self.count("s5"), self.count("m1")), (12, 1))
        self.h.maintain(now=T0 + 60 + 24 * 3600 + 10)           # s5 expired, m1 still kept
        self.assertEqual((self.count("s5"), self.count("m1")), (0, 1))
        self.h.maintain(now=T0 + 60 + 7 * 24 * 3600 + 10)
        self.assertEqual(self.count("m1"), 0)

    def test_query_tier_and_point_budget(self):
        now = T0 + 90 * 24 * 3600
        for table, res in (("s5", 5), ("m1", 60), ("m15", 900)):
            mid = self.h._metric_id("x")
            span = {"s5": 24 * 3600, "m1": 7 * 24 * 3600, "m15": 90 * 24 * 3600}[table]
            rows = [(mid, ts, 1.0) if table == "s5" else (mid, ts, 1.0, 2.0) for ts in range(now - span, now, res)]
            self.h._db.executemany(f"INSERT INTO {table} VALUES ({'?,?,?' if table == 's5' else '?,?,?,?'})", rows)
        for range_s, tier in ((60, "s5"), (3600, "s5"), (6 * 3600, "s5"), (10 * 3600, "m1"), (24 * 3600, "m1"),
                              (7 * 24 * 3600, "m15"), (30 * 24 * 3600, "m15"), (90 * 24 * 3600, "m15"),
                              (999 * 24 * 3600, "m15")):
            q = self.h.query(["x"], range_s, end=now)
            pts = q["series"]["x"]
            self.assertEqual(q["tier"], tier, range_s)
            self.assertLessEqual(len(pts), MAX_POINTS + 1, range_s)
            self.assertGreater(len(pts), 10 if range_s == 60 else 300, range_s)
            self.assertEqual(q["step_s"] % {"s5": 5, "m1": 60, "m15": 900}[tier], 0)

    def test_query_keeps_spikes_in_max_and_ignores_unknown_metrics(self):
        self.fill(T0, 3600, lambda ts: 100.0 if ts == T0 + 1000 else 1.0)
        self.h.maintain(now=T0 + 3600)
        q = self.h.query(["x", "nope"], 3600, end=T0 + 3600)
        self.assertEqual(q["series"]["nope"], [])
        self.assertEqual(max(p[2] for p in q["series"]["x"]), 100.0)
        self.assertLess(max(p[1] for p in q["series"]["x"]), 100.0)

    def test_failed_flush_keeps_samples(self):
        self.h.record(T0, {"a": 1.0})
        real = self.h._metric_id
        self.h._metric_id = lambda name: (_ for _ in ()).throw(RuntimeError("disk full"))
        with self.assertRaises(RuntimeError):
            self.h.flush()
        self.assertFalse(self.h._db.in_transaction)
        self.h._metric_id = real
        self.assertEqual(self.h.flush(), 1)

    def test_events(self):
        self.h.add_event("action", "smbd", "info", "restarted", ts=T0)
        self.h.add_event("alert", "disk /", "crit", "93%", ts=T0 + 10)
        ev = self.h.events()
        self.assertEqual([e["subject"] for e in ev], ["disk /", "smbd"])     # newest first
        self.assertEqual(len(self.h.events(since=T0 + 5)), 1)
        self.h.maintain(now=T0 + 10 + 91 * 24 * 3600)
        self.assertEqual(self.h.events(), [])


class ExtractMetricsTest(unittest.TestCase):
    def test_real_collectors(self):
        cfg, _ = config.load(allow_example=True)
        cs = collectors.create()
        names = ["cpu", "memory", "disk_io", "net_io", "disks"]
        import time
        snap = {n: collectors.run(cs[n], cfg) for n in names}
        time.sleep(0.3)
        snap = {n: collectors.run(cs[n], cfg) for n in names}
        m = extract_metrics(snap)
        for key in ("cpu.busy", "cpu.steal", "load.1", "mem.used_pct", "mem.swap_in_Bps", "psi.io_full"):
            self.assertIn(key, m)
        self.assertTrue(any(k.startswith("io.sda.") for k in m))
        self.assertTrue(any(k.startswith("fs./.") for k in m))
        self.assertTrue(all(isinstance(v, (int, float)) for v in m.values()))

    def test_errors_and_missing_rates_leave_gaps_not_crashes(self):
        snap = {"cpu": {"error": "boom"}, "memory": None,
                "disk_io": {"devices": [{"device": "sda", "read_Bps": None, "busy_pct": 3.0}],
                            "pressure": {"some": {"avg10": 1.0}, "full": {"avg10": 0.5}}}}
        m = extract_metrics(snap)
        self.assertEqual(m["io.sda.busy_pct"], 3.0)
        self.assertNotIn("io.sda.read_Bps", m)
        self.assertFalse(any(k.startswith("cpu.") for k in m))


if __name__ == "__main__":
    unittest.main()
