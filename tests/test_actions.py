import ctypes
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import unittest

from dashboard import actions as act
from dashboard import config
from dashboard.actions import ActionError, Actions, read_identity
from dashboard.audit import Audit
from dashboard.util import CommandError

CFG = json.loads(config.EXAMPLE_PATH.read_text())
WHO = {"user": "tester", "ip": "10.0.0.1"}


def setUpModule():
    logging.disable(logging.CRITICAL)
    act.EXIT_WAIT_S = 0.3


def tearDownModule():
    logging.disable(logging.NOTSET)


class FakeScheduler:
    def __init__(self, log):
        self.log = log
        self.systemd = {"watched": [
            {"name": n, "exists": True, "active": "active", "sub": "running"}
            for n in ("smbd", "ssh", "nginx", "tailscaled", "server-dashboard", "admin-dns", "redis-server")]
            + [{"name": "gone", "exists": False, "active": "inactive", "sub": "dead"}]}
        self.supervisor = {"programs": [{"name": "queue-worker:site-worker_00", "state": "RUNNING"}]}

    def get(self, name):
        return 1.0, {"systemd": self.systemd, "supervisor": self.supervisor}[name]

    def refresh(self, name):
        self.log.append(("refresh", name))
        return self.systemd if name == "systemd" else self.supervisor


class FakeAlerts:
    def __init__(self, log):
        self.log, self.events = log, []

    def mute(self, alert_id, minutes=None, until_clear=False):
        self.log.append(("mute", alert_id, until_clear))

    def event(self, key, text, level="info", cooldown_s=900):
        self.events.append((level, text))
        return True


class FoundationTest(unittest.TestCase):
    """What the admin tools of later phases build on: registered actions, background jobs, typed confirmation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit = Audit(os.path.join(self.tmp.name, "audit.jsonl"))
        self.events = []
        alerts = FakeAlerts([])
        alerts.events = self.events
        self.acts = Actions(CFG, FakeScheduler([]), alerts, self.audit, runner=lambda *a, **k: "")

    def entries(self):
        return self.audit.tail(50)

    def test_a_registered_action_is_audited_and_announced_like_the_built_in_ones(self):
        self.acts.register("demo", lambda body: {"detail": "did it"}, lambda body: {"action": "demo.run", "target": body["what"]})
        self.assertEqual(self.acts.perform("demo", {"what": "thing"}, WHO)["detail"], "did it")
        e = self.entries()[0]
        self.assertEqual((e["action"], e["target"], e["ok"], e["user"]), ("demo.run", "thing", True, "tester"))
        self.assertIn("demo.run on thing", self.events[-1][1])

    def test_a_refused_registered_action_is_audited_and_403_is_announced(self):
        def handler(body):
            raise ActionError(403, "not allowed")
        self.acts.register("demo", handler, lambda body: {"action": "demo.run", "target": "t"})
        with self.assertRaises(ActionError):
            self.acts.perform("demo", {}, WHO)
        self.assertEqual((self.entries()[0]["ok"], self.entries()[0]["detail"]), (False, "not allowed"))
        self.assertEqual(self.events[-1][0], "warn")

    def test_register_rejects_duplicates_and_a_broken_label_cannot_stop_auditing(self):
        self.acts.register("demo", lambda body: {"detail": "x"}, lambda body: body["missing"])
        with self.assertRaises(ValueError):
            self.acts.register("demo", lambda body: {}, lambda body: {})
        with self.assertRaises(ValueError):
            self.acts.register("service", lambda body: {}, lambda body: {})
        self.acts.perform("demo", {}, WHO)
        self.assertEqual(self.entries()[0]["action"], "demo")

    def test_labels_are_cleaned_for_the_audit_log_and_telegram(self):
        self.acts.register("demo", lambda body: {"detail": "x"},
                           lambda body: {"action": "demo.run", "target": "line1\nline2\x1b[0m " + "z" * 500})
        self.acts.perform("demo", {}, WHO)
        target = self.entries()[0]["target"]
        self.assertNotIn("\n", target)
        self.assertNotIn("\x1b", target)
        self.assertLessEqual(len(target), 200)

    def test_clean_and_require_confirmation(self):
        self.assertEqual(act.clean("a\nb\tc\x00d", 10), "a b c d")
        self.assertEqual(len(act.clean("x" * 500, 100)), 100)
        act.require_confirmation({"confirm": "myhost"}, "myhost")
        act.require_confirmation({"confirm": "  myhost \n"}, "myhost")
        for bad in ({}, {"confirm": ""}, {"confirm": "MYHOST"}, {"confirm": "myhost2"}, {"confirm": None}, {"confirm": ["myhost"]}, {"confirm": 5}):
            with self.assertRaises(ActionError, msg=repr(bad)) as raised:
                act.require_confirmation(bad, "myhost")
            self.assertEqual(raised.exception.status, 400)

    def test_a_job_runs_in_the_background_holds_the_lock_and_is_audited(self):
        release, started = threading.Event(), threading.Event()

        def work(job):
            job.step("working")
            started.set()
            release.wait(5)
            return "all done"

        self.acts.register("demo", lambda body: {"detail": "x"}, lambda body: {"action": "demo.job", "target": "t"})
        job = self.acts.run_job("demo", {}, WHO, work)
        self.assertTrue(started.wait(5))
        self.assertEqual(self.acts.jobs.get(job.id)["state"], "running")
        with self.assertRaises(ActionError) as busy:                         # nothing else may run meanwhile
            self.acts.perform("demo", {}, WHO)
        self.assertEqual(busy.exception.status, 409)
        with self.assertRaises(ActionError) as busy_job:
            self.acts.run_job("demo", {}, WHO, work)
        self.assertEqual(busy_job.exception.status, 409)
        release.set()
        for _ in range(100):
            if job.done and not self.acts._lock.locked():
                break
            threading.Event().wait(0.02)
        d = self.acts.jobs.get(job.id)
        self.assertEqual((d["state"], d["detail"], d["started_by"]), ("ok", "all done", "tester"))
        self.assertEqual((self.entries()[0]["action"], self.entries()[0]["ok"], self.entries()[0]["detail"]), ("demo.job", True, "all done"))
        self.assertEqual(self.acts.perform("demo", {}, WHO)["detail"], "x")        # the lock is free again

    def run_to_end(self, work):
        job = self.acts.run_job("demo", {}, WHO, work)
        for _ in range(200):
            if job.done and not self.acts._lock.locked():
                break
            threading.Event().wait(0.02)
        return job.to_dict()

    def test_a_job_that_raises_is_reported_and_releases_the_lock(self):
        self.acts.register("demo", lambda body: {"detail": "x"}, lambda body: {"action": "demo.job", "target": "t"})

        def refuses(job):
            raise ActionError(403, "refused by rule")

        d = self.run_to_end(refuses)
        self.assertEqual((d["state"], d["detail"]), ("failed", "refused by rule"))
        self.assertEqual((self.entries()[0]["ok"], self.entries()[0]["detail"]), (False, "refused by rule"))
        self.assertEqual(self.events[-1][0], "warn")

        def crashes(job):
            raise RuntimeError("secret internals")

        d = self.run_to_end(crashes)
        self.assertEqual(d["state"], "failed")
        self.assertNotIn("secret internals", str(d))                         # the message never reaches the page
        self.assertFalse(self.acts._lock.locked())


class Case(unittest.TestCase):
    def setUp(self):
        self.log = []
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = Audit(os.path.join(self.tmp.name, "audit.jsonl"))
        self.alerts = FakeAlerts(self.log)
        self.sched = FakeScheduler(self.log)
        self.output = ""
        self.fail = None

        def runner(args, timeout=5.0, ok_codes=(0,)):
            self.log.append(("run", args))
            if self.fail:
                raise self.fail
            return self.output

        self.acts = Actions(CFG, self.sched, self.alerts, self.audit, runner=runner)
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait()
            p.stdout.close()
        self.tmp.cleanup()

    def spawn(self, code="import time; time.sleep(60)", comm=None):
        """Start a throwaway process and return only once it has finished setting itself up."""
        if "ready" not in code:
            code = "print('ready', flush=True); " + code
        if comm:                                     # rename first, THEN announce readiness
            code = f"import ctypes; ctypes.CDLL(None).prctl(15, {comm!r}.encode()); " + code
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.procs.append(p)
        self.assertEqual(p.stdout.readline().strip(), "ready")
        return p

    def svc(self, name, op, kind="systemd"):
        return self.acts.perform("service", {"kind": kind, "name": name, "op": op}, WHO)

    def refused(self, kind, body, status):
        with self.assertRaises(ActionError) as ctx:
            self.acts.perform(kind, body, WHO)
        self.assertEqual(ctx.exception.status, status, ctx.exception.message)
        return ctx.exception.message

    def kill(self, p, sig="TERM", **extra):
        ident = read_identity(p.pid)
        return self.acts.perform("process", {"pid": p.pid, "signal": sig, "start_ticks": ident["start_ticks"], "name": ident["comm"], **extra}, WHO)


class ServiceTest(Case):
    def test_restart_uses_an_argument_list_with_option_terminator_and_refreshes(self):
        r = self.svc("smbd", "restart")
        self.assertEqual(self.log, [("run", ["systemctl", "restart", "--", "smbd.service"]), ("refresh", "systemd")])
        self.assertTrue(r["ok"]); self.assertIn("smbd", r["detail"])

    def test_stop_mutes_the_alert_before_stopping(self):
        self.svc("redis-server", "stop")
        self.assertEqual(self.log[0], ("mute", "unit:redis-server", True))
        self.assertEqual(self.log[1], ("run", ["systemctl", "stop", "--", "redis-server.service"]))
        self.log.clear(); self.svc("redis-server", "start"); self.svc("redis-server", "restart")
        self.assertNotIn("mute", [e[0] for e in self.log])

    def test_protected_services_can_be_restarted_but_never_stopped(self):
        for name in ("ssh", "nginx", "tailscaled", "server-dashboard", "admin-dns"):
            self.log.clear()
            self.assertIn("protected", self.refused("service", {"kind": "systemd", "name": name, "op": "stop"}, 403))
            self.assertEqual(self.log, [], f"{name}: nothing may run or be muted")
        self.svc("nginx", "restart")
        self.assertEqual(self.log[0], ("run", ["systemctl", "restart", "--", "nginx.service"]))

    def test_the_dashboard_restarts_itself_without_waiting(self):
        r = self.svc("server-dashboard", "restart")
        self.assertEqual(self.log, [("run", ["systemctl", "restart", "--no-block", "--", "server-dashboard.service"])])
        self.assertTrue(r["reconnect"])
        self.refused("service", {"kind": "systemd", "name": "server-dashboard", "op": "start"}, 400)

    def test_only_listed_and_installed_services(self):
        self.refused("service", {"kind": "systemd", "name": "cron", "op": "restart"}, 404)       # real unit, not on the watch list
        self.refused("service", {"kind": "systemd", "name": "gone", "op": "restart"}, 404)       # listed but not installed
        self.assertEqual(self.log, [])

    def test_hostile_or_malformed_input_never_reaches_a_command(self):
        for name in ("smbd; reboot", "-rf", "--now", "../x", "a b", "", "smbd\n", "x" * 200, "$(id)", "smbd.service`id`"):
            self.refused("service", {"kind": "systemd", "name": name, "op": "restart"}, 400)
        for body in ({"kind": "systemd", "name": "smbd", "op": "disable"}, {"kind": "systemd", "name": "smbd", "op": "mask"},
                     {"kind": "shell", "name": "smbd", "op": "restart"}, {"kind": "systemd", "name": 5, "op": "restart"},
                     {"kind": None, "name": "smbd", "op": "restart"}, {}):
            self.refused("service", body, 400)
        self.assertEqual(self.log, [])

    def test_a_failing_command_is_reported(self):
        self.fail = CommandError("systemctl: exit 5: Unit not loaded")
        self.assertIn("failed", self.refused("service", {"kind": "systemd", "name": "smbd", "op": "start"}, 502))

    def test_supervisor(self):
        name = "queue-worker:site-worker_00"
        self.output = "queue-worker:site-worker_00: stopped"
        r = self.svc(name, "stop", kind="supervisor")
        self.assertEqual(self.log[0], ("mute", f"supervisor:{name}", True))
        self.assertEqual(self.log[1], ("run", ["supervisorctl", "stop", name]))
        self.assertIn("stopped", r["detail"])
        self.refused("service", {"kind": "supervisor", "name": "other", "op": "stop"}, 404)
        self.output = "queue-worker:site-worker_00: ERROR (already started)"
        self.assertIn("ERROR", self.refused("service", {"kind": "supervisor", "name": name, "op": "start"}, 502))


class ProcessTest(Case):
    def test_term_ends_a_normal_process(self):
        p = self.spawn()
        r = self.kill(p)
        self.assertTrue(r["exited"]); self.assertIn("it exited", r["detail"])
        p.wait(timeout=5)
        self.assertEqual(p.returncode, -15)

    def test_a_process_that_ignores_term_needs_kill(self):
        p = self.spawn("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)")
        self.assertFalse(self.kill(p, "TERM")["exited"])
        self.assertIn("still running", self.kill(p, "TERM")["detail"])
        self.assertTrue(self.kill(p, "KILL")["exited"])
        p.wait(timeout=5)
        self.assertEqual(p.returncode, -9)

    def test_a_recycled_pid_is_never_signalled(self):
        p = self.spawn()
        ident = read_identity(p.pid)
        self.assertIn("different process", self.refused("process", {"pid": p.pid, "signal": "KILL", "start_ticks": ident["start_ticks"] + 1}, 409))
        self.assertIn("name does not match", self.refused("process", {"pid": p.pid, "signal": "KILL", "start_ticks": ident["start_ticks"], "name": "nginx"}, 409))
        self.assertIsNone(p.poll(), "the process must be untouched")

    def test_a_process_that_is_gone(self):
        p = self.spawn(); pid = p.pid; ticks = read_identity(pid)["start_ticks"]
        p.kill(); p.wait()
        self.refused("process", {"pid": pid, "signal": "TERM", "start_ticks": ticks}, 404)

    def test_pid_1_kernel_threads_and_ourselves_are_refused(self):
        self.assertIn("PID 1", self.refused("process", {"pid": 1, "signal": "KILL", "start_ticks": 0}, 403))
        kthreadd = read_identity(2)
        self.assertIn("kernel thread", self.refused("process", {"pid": 2, "signal": "KILL", "start_ticks": kthreadd["start_ticks"]}, 403))
        child_of_kthreadd = next(int(d) for d in os.listdir("/proc") if d.isdigit() and int(d) > 2 and _ppid(int(d)) == 2)
        ident = read_identity(child_of_kthreadd)
        self.assertIn("kernel thread", self.refused("process", {"pid": child_of_kthreadd, "signal": "TERM", "start_ticks": ident["start_ticks"]}, 403))
        me = read_identity(os.getpid())
        self.assertIn("dashboard itself", self.refused("process", {"pid": os.getpid(), "signal": "TERM", "start_ticks": me["start_ticks"]}, 403))
        if os.getppid() > 1:
            parent = read_identity(os.getppid())
            self.assertIn("dashboard itself", self.refused("process", {"pid": os.getppid(), "signal": "TERM", "start_ticks": parent["start_ticks"]}, 403))

    def test_core_system_daemons_are_refused_by_name(self):
        for comm in ("systemd-fake", "sshd", "tailscaled", "init"):
            p = self.spawn(comm=comm)
            self.assertIn("core system process", self.refused("process", {"pid": p.pid, "signal": "KILL", "start_ticks": read_identity(p.pid)["start_ticks"]}, 403), comm)
            self.assertIsNone(p.poll())

    def test_protection_by_service_membership(self):
        base = {"pid": 4000, "ppid": 1, "flags": 0, "comm": "worker", "start_ticks": 1}
        for unit in ("ssh.service", "nginx.service", "tailscaled.service", "server-dashboard.service", "admin-dns.service"):
            self.assertIn("protected service", self.acts.protected_reason({**base, "unit": unit}), unit)
        for unit in ("app1.service", "session-3.scope", "", "smbd.service", "ssh-agent.service"):
            self.assertIsNone(self.acts.protected_reason({**base, "unit": unit}), unit)

    def test_bad_input(self):
        for body in ({}, {"pid": "5", "signal": "TERM", "start_ticks": 1}, {"pid": True, "signal": "TERM", "start_ticks": 1},
                     {"pid": 5, "signal": "HUP", "start_ticks": 1}, {"pid": 5, "signal": "TERM"}, {"pid": 5, "signal": "TERM", "start_ticks": -1},
                     {"pid": 5, "signal": "TERM", "start_ticks": "1"}, {"pid": 0, "signal": "TERM", "start_ticks": 1},
                     {"pid": -3, "signal": "TERM", "start_ticks": 1}, {"pid": 2 ** 40, "signal": "TERM", "start_ticks": 1},
                     {"pid": 5, "signal": "TERM", "start_ticks": 1, "name": 5}):
            self.refused("process", body, 400)

    @unittest.skipIf(os.geteuid() == 0, "would really signal a root-owned process")
    def test_the_operating_system_can_still_say_no(self):
        pid = next((int(d) for d in os.listdir("/proc") if d.isdigit() and open(f"/proc/{d}/comm").read().strip() == "cron"), None)
        if pid is None:
            self.skipTest("no cron process")
        ident = read_identity(pid)
        self.assertIn("operating system", self.refused("process", {"pid": pid, "signal": "TERM", "start_ticks": ident["start_ticks"]}, 403))


def _ppid(pid):
    try:
        return read_identity(pid)["ppid"]
    except ProcessLookupError:
        return -1


class AuditAndLimitsTest(Case):
    def test_every_outcome_is_audited(self):
        self.svc("smbd", "restart")
        self.refused("service", {"kind": "systemd", "name": "ssh", "op": "stop"}, 403)
        self.refused("service", {"kind": "systemd", "name": "bad name", "op": "stop"}, 400)
        entries = self.audit.tail(10)
        self.assertEqual([(e["action"], e["ok"], e["result"]) for e in entries],
                         [("service.stop", False, "refused (400)"), ("service.stop", False, "refused (403)"), ("service.restart", True, "done")])
        self.assertEqual((entries[2]["user"], entries[2]["ip"], entries[2]["target"]), ("tester", "10.0.0.1", "systemd:smbd"))

    def test_telegram_is_told_about_actions_and_refusals_but_not_typos(self):
        self.svc("smbd", "restart")
        self.refused("service", {"kind": "systemd", "name": "ssh", "op": "stop"}, 403)
        self.refused("service", {"kind": "systemd", "name": "bad name", "op": "stop"}, 400)
        self.refused("service", {"kind": "systemd", "name": "cron", "op": "stop"}, 404)
        self.assertEqual([lvl for lvl, _ in self.alerts.events], ["info", "warn"])
        self.assertIn("tester did service.restart on systemd:smbd", self.alerts.events[0][1])

    def test_only_one_action_at_a_time(self):
        self.acts._lock.acquire()
        try:
            self.assertIn("another action", self.refused("service", {"kind": "systemd", "name": "smbd", "op": "restart"}, 409))
        finally:
            self.acts._lock.release()

    def test_rate_limit(self):
        for _ in range(act.MAX_ACTIONS_PER_MINUTE):
            self.svc("smbd", "start")
        self.refused("service", {"kind": "systemd", "name": "smbd", "op": "start"}, 429)

    def test_an_unexpected_bug_is_contained_audited_and_hidden(self):
        self.sched.get = lambda name: (_ for _ in ()).throw(RuntimeError("secret internals"))
        msg = self.refused("service", {"kind": "systemd", "name": "smbd", "op": "start"}, 500)
        self.assertEqual(msg, "internal error")
        self.assertEqual(self.audit.tail(1)[0]["detail"], "internal error: RuntimeError")


class AuditFileTest(unittest.TestCase):
    def test_file_mode_rotation_order_and_resilience(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "audit.jsonl")
            a = Audit(path, max_bytes=600)
            for i in range(30):
                a.record("u", "1.2.3.4", "service.restart", f"systemd:svc{i}", True, "done", "x" * 20)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertTrue(os.path.exists(path + ".1")); self.assertTrue(os.path.exists(path + ".2")); self.assertFalse(os.path.exists(path + ".4"))
            tail = a.tail(5)
            self.assertEqual([e["target"] for e in tail], [f"systemd:svc{i}" for i in range(29, 24, -1)])      # newest first
            with open(path, "a") as f:
                f.write("{torn line\n")
            self.assertEqual(a.tail(1)[0]["target"], "systemd:svc29")                                          # a torn line is skipped
            self.assertTrue(all(json.loads(l) for l in open(path).read().splitlines()[:-1]))

    def test_an_unwritable_log_never_blocks_the_action(self):
        a = Audit("/nonexistent-directory/audit.jsonl")
        self.assertEqual(a.record("u", "i", "a", "t", True, "r")["action"], "a")

    def test_long_values_are_truncated(self):
        with tempfile.TemporaryDirectory() as d:
            e = Audit(os.path.join(d, "a.jsonl")).record("u" * 500, "i", "a", "t" * 5000, True, "r" * 5000, "d" * 5000)
            self.assertLess(len(json.dumps(e)), 1500)


if __name__ == "__main__":
    unittest.main()
