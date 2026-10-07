import unittest

from dashboard.jobs import MAX_STEPS, Jobs


class JobTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.jobs = Jobs(keep=3, clock=lambda: self.now)

    def test_steps_progress_and_the_job_finishes(self):
        job = self.jobs.create("restart-all", "restart all services", "alice")
        self.assertEqual(job.to_dict()["state"], "running")
        job.step("redis-server")
        job.step("nginx")
        self.now += 5
        job.finish(True, "2 ok")
        d = self.jobs.get(job.id)
        self.assertEqual([(s["label"], s["state"]) for s in d["steps"]], [("redis-server", "ok"), ("nginx", "ok")])
        self.assertEqual((d["state"], d["detail"], d["started_by"], d["finished"]), ("ok", "2 ok", "alice", 1005.0))
        self.assertTrue(job.done)

    def test_a_failed_step_does_not_end_the_job_but_a_failed_finish_marks_the_running_step(self):
        job = self.jobs.create("x", "x", "u")
        job.step("one")
        job.step_failed("exited with 1")
        job.step("two")
        job.finish(False, "stopped here")
        d = job.to_dict()
        self.assertEqual([(s["state"], s["detail"]) for s in d["steps"]], [("failed", "exited with 1"), ("failed", "stopped here")])
        self.assertEqual(d["state"], "failed")

    def test_finish_is_final(self):
        job = self.jobs.create("x", "x", "u")
        job.finish(False, "first")
        job.finish(True, "second")
        self.assertEqual((job.state, job.detail), ("failed", "first"))

    def test_note_sets_the_current_steps_text(self):
        job = self.jobs.create("x", "x", "u")
        job.note("ignored: no step yet")
        job.step("check")
        job.note("nginx: syntax ok")
        self.assertEqual(job.to_dict()["steps"][0]["detail"], "nginx: syntax ok")

    def test_text_is_one_printable_line_and_bounded(self):
        job = self.jobs.create("x", "label\nwith\x1b[31m control", "u\nser")
        job.step("a" * 1000, "b" * 1000)
        d = job.to_dict()
        self.assertNotIn("\n", d["label"] + d["started_by"])
        self.assertNotIn("\x1b", d["label"])
        self.assertEqual((len(d["steps"][0]["label"]), len(d["steps"][0]["detail"])), (200, 500))

    def test_step_count_is_bounded(self):
        job = self.jobs.create("x", "x", "u")
        for i in range(MAX_STEPS + 50):
            job.step(f"s{i}")
        self.assertEqual(len(job.to_dict()["steps"]), MAX_STEPS)

    def test_old_finished_jobs_are_dropped_but_running_ones_never(self):
        running = self.jobs.create("x", "running", "u")
        done = []
        for i in range(5):
            j = self.jobs.create("x", f"done{i}", "u")
            j.finish(True)
            done.append(j)
        self.assertIsNotNone(self.jobs.get(running.id))
        self.assertIsNone(self.jobs.get(done[0].id))
        self.assertIsNotNone(self.jobs.get(done[-1].id))

    def test_ids_are_random_hex_and_unknown_ids_are_none(self):
        a, b = self.jobs.create("x", "x", "u"), self.jobs.create("x", "x", "u")
        self.assertRegex(a.id, r"^[0-9a-f]{16}$")
        self.assertNotEqual(a.id, b.id)
        self.assertIsNone(self.jobs.get("0" * 16))


if __name__ == "__main__":
    unittest.main()
